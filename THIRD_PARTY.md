# 上游来源

上游目录保持原样；新工具位于根目录的 `src/autodl_grab`，安装后无需两个上游仓库。

| 来源 | 本地核对的提交 | 采用内容 |
| --- | --- | --- |
| [grenbel/autodl-autogpu](https://github.com/grenbel/autodl-autogpu) | `5cd650f72fea3793357b15e3e8618cb10fc41d83` | `reference/console.js` 改造为 `assets/console.js`；`reference/clone-page.js` 改造为 `assets/create.js`；`tests/console/fixtures.js`、`clone_fixtures.js` 作为虚构 DOM 样本 |
| [PYgdMIE/kuake-pipe](https://github.com/PYgdMIE/kuake-pipe) | `eea22d17c608d42950310e688a8645abf77d2511` | `src/kuake/autodl_api.py` 的只读 URL、查询参数、JWT 存储位置及千分之一元计价约定；重新实现分页及错误处理 |

两者均为 MIT。完整许可证分别保存在安装包的
`autodl_grab/licenses/autodl-autogpu-MIT.txt` 与 `kuake-pipe-MIT.txt`。

`console.js` 的修改：完整 ID 定位只作用于已识别实例表的主表体，忽略 Element UI 在主表体外复制的固定列、提示和其他控件；主表内出现真实重复 ID 仍拒绝操作。添加只读 `locate` 方法，把主表 ID 集合与本次页面查询返回的 UUID 集合严格对齐，供 Python 等待渲染和分页。确认框、按钮、实例规格和其他行状态的提交前复核保留；脚本版本递增为 11。

`create.js` 的修改：允许 Python 已筛选的替代型号，取消必须在同一筛选表中找到源型号主机的限制；暴露只读原始主机行；加入新租页面的按量、地区和指定镜像检查。型号白名单、继承/覆盖的最低要求及整实例预算在 Python 层检查。原来的完整字段比对、页面重新读取、两分钟时效、按钮遮挡检查和单次最终提交检查保留。不要用上游原文件哈希校验这个已改造文件。

0.1.6 将主机表加载和费用更新返回为显式 `pending`，为最终点击前的拒绝增加 `notSubmitted` 证据，并适配固定主机新租表单的路由、镜像、数量和零扩容控件。`console.js` 暴露源实例克隆菜单是否可操作的只读证据，并区分最终点击前拒绝与已经尝试提交；最终按钮仍只允许点击一次。0.1.7 在创建守卫中按当前页面报价校验金额与请求代次，新增的 `quote.js` 为本项目实现的只读报价观察器。

没有引入上游的 AI Skill 调度、SSH、开机钩子、克隆票、训练脚本、自动关机、夸克及网盘初始化模块。

复制完成条件依据 [AutoDL 官方同地区迁移说明](https://www.autodl.com/docs/migrate_instance_2/)：系统盘复制后数据盘还可能继续复制。本工具必须另行看到数据盘完成及源实例解除克隆锁，才标记成功。上游的小数据量实测不等于本项目的实机验收。
