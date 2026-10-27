import json
import sqlite3
from dataclasses import replace

import pytest

from autodl_grab import cli
from autodl_grab.errors import Pause
from autodl_grab.store import Store, profile_lock


def submit(env, state='pending'):
    action = env.store.intent(env.task, 'start', None, {env.config.source_id}, {})
    if state != 'pending':
        env.store.action_update(action, state, env.config.source_id)
    return action


def test_cancel_backs_up_and_preserves_history_and_login(env):
    login = env.home / 'storage_state.json'
    login.write_text('fixture-session', encoding='utf-8')
    result = env.store.cancel()
    assert result['id'] == env.task
    assert result['phase'] == 'cancelled'
    assert not result['forced']
    with sqlite3.connect(result['backup']) as backup:
        assert backup.execute('SELECT phase FROM tasks WHERE id=?', (env.task,)).fetchone() == ('polling',)
        assert backup.execute('PRAGMA integrity_check').fetchone() == ('ok',)
    assert env.store.active() == []
    assert env.store.get(env.task)['phase'] == 'cancelled'
    assert login.read_text(encoding='utf-8') == 'fixture-session'
    assert env.store.new(env.config) != env.task
    assert any(json.loads(line).get('phase') == 'cancelled'
               for line in (env.home / 'events.jsonl').read_text(encoding='utf-8').splitlines())


@pytest.mark.parametrize('state', ['pending', 'accepted'])
@pytest.mark.parametrize('operation', ['cancel', 'restart'])
def test_pending_submission_requires_force_and_is_preserved(env, state, operation):
    submit(env, state)
    before = env.store.pending(env.task)
    call = lambda force: (env.store.cancel(force=force) if operation == 'cancel'
                          else env.store.restart(env.config, force=force))
    with pytest.raises(Pause, match='--force') as caught:
        call(False)
    assert env.task in str(caught.value)
    assert 'resume' in str(caught.value)
    assert env.store.get(env.task)['phase'] == 'submitted'
    assert not (env.home / 'backups').exists()
    result = call(True)
    cancelled = result if operation == 'cancel' else result[1]
    assert cancelled['forced'] is True
    assert env.store.pending(env.task) == before
    assert env.store.get(env.task)['phase'] == 'cancelled'
    with sqlite3.connect(cancelled['backup']) as backup:
        assert backup.execute('SELECT state FROM actions').fetchone() == (state,)


@pytest.mark.parametrize('state', ['rejected', 'aborted', 'done'])
def test_resolved_submission_does_not_need_force(env, state):
    submit(env, state)
    assert env.store.cancel()['forced'] is False


@pytest.mark.parametrize('phase', ['complete', 'cancelled'])
def test_cancel_terminal_task_is_idempotent(env, phase):
    env.store.update(env.task, phase=phase)
    assert env.store.cancel(env.task)['phase'] == phase
    assert env.store.cancel()['id'] is None
    assert not (env.home / 'backups').exists()


def test_unknown_task_is_error(env):
    with pytest.raises(Pause, match='不存在'):
        env.store.cancel('missing')
    assert len(env.store.active()) == 1


def test_multiple_tasks_require_explicit_cancel(env):
    # Simulate a legacy/imported database with multiple unfinished records.
    with env.store.db:
        other = env.store._insert(env.config)
    with pytest.raises(Pause, match='多个'):
        env.store.cancel(force=True)
    with pytest.raises(Pause, match='多个'):
        env.store.restart(env.config, force=True)
    assert len(env.store.active()) == 2
    env.store.cancel(other)
    assert [row['id'] for row in env.store.active()] == [env.task]


@pytest.mark.parametrize('operation', ['cancel', 'restart'])
def test_backup_failure_does_not_modify_tasks(env, operation):
    (env.home / 'backups').write_text('blocked', encoding='utf-8')
    before = env.store.all()
    with pytest.raises(Pause, match='备份失败'):
        if operation == 'cancel':
            env.store.cancel()
        else:
            env.store.restart(env.config)
    assert env.store.all() == before


def test_restart_uses_current_config_and_new_deadline(env):
    env.store.update(env.task, phase='paused')
    with env.store.db:
        env.store.db.execute('UPDATE tasks SET created=1 WHERE id=?', (env.task,))
    config = replace(env.config, max_wait_seconds=123, max_hourly='4')
    task, cancelled = env.store.restart(config)
    assert task != env.task
    assert cancelled['id'] == env.task
    row = env.store.get(task)
    assert row['created'] > 1
    assert row['phase'] == 'polling'
    assert json.loads(row['config']) == config.snapshot()
    assert [r['id'] for r in env.store.active()] == [task]


def test_restart_without_active_task_creates_one_without_backup(env):
    env.store.update(env.task, phase='complete')
    task, cancelled = env.store.restart(env.config)
    assert cancelled is None
    assert env.store.get(task)['phase'] == 'polling'
    assert not (env.home / 'backups').exists()


def test_restart_rolls_back_cancellation_if_insert_fails(env, monkeypatch):
    before = env.store.all()

    def fail(config):
        raise sqlite3.IntegrityError('fixture insertion failure')

    monkeypatch.setattr(env.store, '_insert', fail)
    with pytest.raises(sqlite3.IntegrityError):
        env.store.restart(env.config)
    assert env.store.all() == before


def no_browser(*args, **kwargs):
    pytest.fail('local task management must not construct a browser')


def test_cli_cancel_and_status_preserve_history(env, monkeypatch, capsys):
    monkeypatch.setattr(cli, 'Browser', no_browser)
    assert cli.main(['cancel', '--home', str(env.home)]) == 0
    assert json.loads(capsys.readouterr().out)['phase'] == 'cancelled'
    assert cli.main(['--home', str(env.home), 'status']) == 0
    assert json.loads(capsys.readouterr().out)[0]['phase'] == 'cancelled'
    assert cli.main(['--home', str(env.home), 'resume', env.task]) == 2
    assert '已取消' in capsys.readouterr().err
    with pytest.raises(Pause, match='已取消'):
        env.engine.run(env.task)
    assert env.store.get(env.task)['phase'] == 'cancelled'


def test_cli_completed_resume_is_readonly(env, monkeypatch, capsys):
    env.store.update(env.task, phase='complete', result=json.dumps({'ok': True}))
    monkeypatch.setattr(cli, 'Browser', no_browser)
    assert cli.main(['--home', str(env.home), 'resume', env.task]) == 0
    assert json.loads(capsys.readouterr().out) == {'ok': True}


@pytest.mark.parametrize('operation', ['cancel', 'restart'])
def test_cli_lock_conflict_changes_nothing(env, monkeypatch, operation):
    monkeypatch.setattr(cli, 'Browser', no_browser)
    monkeypatch.setattr(cli.Config, 'load', lambda _: env.config)
    args = ['--home', str(env.home), operation]
    if operation == 'restart':
        args += ['-c', 'unused.toml']
    before = env.store.all()
    with profile_lock(env.home):
        assert cli.main(args) == 2
    assert env.store.all() == before


def test_cli_invalid_restart_config_preserves_old_task(env, monkeypatch):
    monkeypatch.setattr(cli, 'Browser', no_browser)
    before = env.store.all()
    assert cli.main(['--home', str(env.home), 'restart', '-c', str(env.home / 'missing.toml')]) == 2
    assert env.store.all() == before
    assert not (env.home / 'backups').exists()


def test_cli_restart_runs_new_task(env, monkeypatch, capsys):
    monkeypatch.setattr(cli.Config, 'load', lambda _: env.config)
    received = []

    class Browser:
        def __init__(self, home, *, headed=False):
            assert home == env.home.resolve()
            assert headed

        def __exit__(self, *args):
            received.append('closed')

    def run(engine, task):
        assert engine.store.get(env.task)['phase'] == 'cancelled'
        assert engine.store.get(task)['phase'] == 'polling'
        received.append(task)
        return {'ok': True}

    monkeypatch.setattr(cli, 'Browser', Browser)
    monkeypatch.setattr(cli.Engine, 'run', run)
    assert cli.main(['restart', '-c', 'unused.toml', '--headed', '--home', str(env.home)]) == 0
    assert received == [env.store.active()[0]['id'], 'closed']
    assert received[0] != env.task
    assert 'backup' in capsys.readouterr().out


def test_cli_forced_cancel_prints_platform_warning(env, monkeypatch, capsys):
    submit(env, 'accepted')
    monkeypatch.setattr(cli, 'Browser', no_browser)
    assert cli.main(['--home', str(env.home), 'cancel', env.task, '--force']) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)['target'] == env.config.source_id
    assert '计费仍可能继续' in captured.err


def test_run_blocker_includes_actionable_commands(env, monkeypatch, capsys):
    monkeypatch.setattr(cli.Config, 'load', lambda _: env.config)
    monkeypatch.setattr(cli, 'Browser', no_browser)
    assert cli.main(['--home', str(env.home), 'run', '-c', 'unused.toml']) == 2
    message = capsys.readouterr().err
    assert env.task in message
    assert all(command in message for command in ('resume', 'cancel', 'restart'))
