# AutoDL Grab

独立的 Python 3.11 + Playwright 命令行工具。配置后按固定流程持续运行：原实例带卡开机 → 同区克隆系统盘和数据盘 → 按配置在其他地区新租。成功一次即结束。无 AI、无夸克，不启动训练，不释放任何实例。

当前交付包含可安装源码、持久化状态机、页面适配器及离线测试。**尚未使用登录账号完成真实开机、克隆或新租验收。** 页面与网页接口没有稳定契约；必要字段或选择器与适配器不一致时会暂停。首次使用必须先执行只读 `plan`，不要把离线测试通过视为实机验收通过。

## 安装与配置

在这个项目目录运行（PowerShell 示例）：

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[test]"
.\.venv\Scripts\python -m playwright install chromium
Copy-Item config.example.toml config.toml
```

之后激活虚拟环境或使用 `.venv\Scripts\autodl-grab.exe`。Linux/macOS 使用对应的 `bin/python` / `bin/autodl-grab`。

编辑 `config.toml`：填写完整源实例 ID、**整实例每小时价格上限**、**单个新增实例磁盘日费用上限**。金额建议写成带引号的十进制字符串。源 GPU 总是第一优先级；`alternate_gpus` 是显式替代白名单，不替你判断训练代码兼容性。卡数保持不变。

CPU 与内存最低值按整实例计算，显存按每张卡计算；不填写的最低值继承源主机规格。驱动和 CUDA 按版本数字比较。克隆候选按 GPU 优先级、整实例价格、空闲卡数排序；新租先按地区优先级，再按 GPU、价格和空闲卡排序。接口价格从千分之一元换算，预算全程使用 `Decimal`。

`other_regions` 使用网页接口的 `region_sign`，不是地区中文名。为空即关闭其他地区新租；启用时必须填写页面可唯一识别的完整 `image`。新租系统盘和数据盘扩容量默认均为 0。克隆继承源系统盘容量和源数据盘所需扩容量，不自动缩容。

配置中的日费上限只限制**一个新增实例**。源实例始终保留，因此其付费扩容费用可能继续产生。扩容日费预估只用于候选展示，最终核对当前规格对应的平台报价和页面金额；报价在原预算内可以继续，超预算候选会跳过并冷却。

## 命令

0.1.7 增加自动恢复：只读网络故障、限流、服务端错误和响应解析失败会退避重试；已知表单暂态最多重建三次，失败候选冷却五分钟后再检查，期间仍优先尝试原实例开机。`recovering` 状态记录故障阶段、恢复次数和下次重试时间。任务截止时间不因恢复重置；登录失效、权限不足、未知结构及目标歧义仍暂停。

根因、测试和实机验收记录见 [RESILIENCE_REPORT.md](RESILIENCE_REPORT.md)。

创建报价改为监听页面自身的 `order/price/preview` 请求，绑定当前主机、卡数、计费方式和磁盘容量，再与页面显示及原预算核对。磁盘费用不再强制等于固定的 0.0066 元/GB/日；例如已核实的 100GB 平台日费为 0.67 元。旧报价、尚未更新的金额和提交前发生的新报价都不能直接用于提交。提交响应丢失时只核对远端，等待实例出现至原操作超时，禁止重复创建。

```powershell
autodl-grab login
autodl-grab login --sub-account
autodl-grab plan -c config.toml
autodl-grab run -c config.toml
autodl-grab status
autodl-grab resume <task-id>
autodl-grab cancel
autodl-grab cancel <task-id>
autodl-grab restart -c config.toml
```

`login` 打开独立 Chromium 窗口，手动登录后在终端按 Enter；工具验证只读实例接口，再保存登录状态。默认无头运行；调试使用 `autodl-grab --headed plan -c config.toml` 或 `--headed run`。

克隆规格预览、克隆和新租准备会每 250 毫秒检查一次 GPU 型号列表，每次最多等待 15 秒，目标型号精确匹配后才继续选择。超时在登录目录生成 `create_diagnostics.json`，准备阶段会重建表单重试，连续三次失败后冷却候选。诊断记录阶段、脱敏页面路径、目标型号、页面型号及勾选状态和等待耗时，不保存整页内容、实例 ID 或登录凭据。源实例、计费方式等校验失败仍立即暂停，不会因等待型号而跳过校验。

浏览器启动失败、页面导航失败或未预期异常会显示失败阶段，并在登录目录生成 `error_diagnostics.json`。该文件仅记录异常类型、程序内代码位置、已识别的网络错误码和错误类别，不包含异常原文、凭据、请求参数值或原始响应。排查时可提供这个文件。`ERR_NETWORK_ACCESS_DENIED` 表示当前运行环境拒绝浏览器网络访问；导航超时可使用 `--headed` 查看页面加载情况。`plan` 不创建任务，因此只执行过 `plan` 时，`status` 输出 `[]` 属于正常情况。

分页校验失败时会显示实际接口、页码和 ID 字段，并生成 `pagination_diagnostics.json`。其中只记录行数、字段名、ID 类型、重复计数及重复记录发生变化的字段名，不记录 ID 或字段值。跨页 ID 重叠或分页总数变化时，最多重查三轮完整列表，每轮均从第一页开始；不合并不同轮次的数据。同页 ID 重复、ID 缺失或持续不稳定时仍会暂停，不能通过跳过重复记录保证完整库存。

子账号使用 `autodl-grab login --sub-account` 直接打开官方 `/subAccountLogin` 入口。每次 `login` 使用全新会话，验证成功后才替换本地登录状态，避免误沿用此前主账号。查询会复用网页实际发出的会话头、账号参数及 GET/POST 方法，不再假定所有账号都使用 `localStorage.token`。主账号需在平台设置中启用子账号的容器实例功能，并授权源实例可见性及所需镜像权限，参见 [官方子账号说明](https://www.autodl.com/docs/c_user/)。这些适配仍需真实子账号验证。

若提示“未捕获网页只读请求”，它只表示尚未识别到查询接口，不能据此判断登录失败或权限不足。程序会在登录目录生成 `login_diagnostics.json`，记录脱敏接口路径、方法、HTTP 状态、错误码和字段名称，不包含请求头、参数、token、Cookie 或响应值。请提供这个文件的内容来排查实际接口；不要提供 `storage_state.json` 或 `session_storage.json`。已识别的权限错误会明确提示主账号检查权限；未知错误会显示可安全分享的 `code`。

默认状态目录为当前项目工作目录的 `.local/`。换工作目录时请明确指定原目录，例如 `autodl-grab --home D:\Project\AutodlExtension\.local status`。`--home` 和 `--headed` 均可放在子命令前后，例如 `autodl-grab plan -c config.toml --headed`。每个登录目录同一时间只有一个修改进程；`status` 可以并行读取。

`plan` 只查询接口、操作筛选器和读取页面，不提交订单，不开关机，不创建任务。市场接口缺少规格时，会打开源实例的同区克隆预览并选择 GPU 型号以读取主机表，不点击“创建并开机”。这项补全要求源实例已关机且仍有可用克隆次数；其他地区接口缺少字段时会明确暂停并保存字段结构诊断。输出源规格、源主机和排序后的候选。候选不是库存预留；实际提交前还会重新检查源主机、候选型号/数量/地区/磁盘、驱动和最终费用。

实例定位会等待当前页面的查询响应，并核对页面主表的 ID 集合已与该响应一致，再读取目标或翻页。完整 ID 必须在主表中唯一；固定列副本或主表外重复显示的 ID 不参与唯一性判断。若仍定位失败，`.local/console_diagnostics.json` 会记录各页行数、是否包含目标、渲染对齐及分页状态，不保存 ID、页面原文或凭据。接口能找到源实例但页面定位失败时，通常需要检查加载、筛选或 DOM 结构，无需先更改已核实的完整 ID。

`run` 是允许按配置自动开机、克隆、新租的显式运行命令，不再逐项弹确认。输出任务 ID，成功结果包含目标完整 ID、地区、GPU/卡数、费用和数据状态。已运行的原实例费用无法重新获取时显示 `null`。其他地区新租明确输出“新租实例，未迁移原数据”。

## 监控和中断恢复

默认每 15 秒轮询，含 ±15% 随机抖动，最长等待 24 小时；开机超时 10 分钟，克隆超时 60 分钟。查询网络错误递增退避；HTTP 429 遵守 `Retry-After`（秒或 HTTP 日期）。长退避分成最多 30 秒的小段，便于中断。

SQLite 使用 WAL 和 FULL 同步。最终提交前落盘操作类型、页面报价、候选和已有实例 ID 集合。优先使用提交响应中的完整目标 ID；丢失响应或进程重启时对照新增实例集合、主机、地区、型号、卡数及克隆来源核对。不能唯一确认时暂停，保留提交记录，禁止再次创建或切到新租。已确认目标后只监控该目标。

`Ctrl+C` 只停止本地监控，平台已开始的创建或拷贝继续执行。用 `status` 查看记录，再用 `resume` 核对远端。重复 `run` 会被未完成任务阻止，并显示任务 ID 和恢复、取消、重新开始的命令。登录过期可先重新 `login`，然后 `resume`。未知页面、未知返回值、余额不足、源无卡模式运行或操作状态不明均暂停，只有明确的库存不足才回退。工具不会自动关闭正在无卡模式运行的源实例。

操作超时后 `resume` 仍可核对并接收已完成目标；不会为超时操作创建第二台实例。未提交任务达到配置的总等待上限后保持暂停。

要放弃旧任务，运行 `autodl-grab cancel`；默认选择唯一未完成任务，也可用 `cancel <task-id>` 指定任务。没有未完成任务，或指定的任务已完成/已取消时，成功返回而不修改记录。要按当前配置重新开始，运行 `autodl-grab restart -c config.toml`；它先验证配置，再取消旧任务并创建新任务，重新计算等待时间。没有未完成任务时直接创建。存在多个未完成任务时，两个命令都会要求先用 `cancel <task-id>` 分别处理。

取消前自动将整个任务数据库备份到登录目录的 `backups/tasks-<UTC时间>-<唯一标识>.sqlite3`，使用 SQLite 备份接口包含 WAL 中的已提交数据。备份失败则停止操作；`restart` 的旧任务取消和新任务创建处于同一事务。取消使用 `cancelled` 状态，保留配置、提交记录、登录信息及其他历史；`status` 仍可查看，已取消的任务不能再 `resume`，也不阻止新任务启动。`cancel` 不打开浏览器或访问平台；两个命令均遵守登录目录的单进程锁。

若旧任务有 `pending` 或 `accepted` 的开机、克隆、新租提交，默认拒绝取消或重新开始，并显示恢复命令和已知目标。先核实 AutoDL 控制台；确定放弃本地跟踪时显式使用 `autodl-grab cancel --force` 或 `autodl-grab restart -c config.toml --force`。强制操作仍备份并保留原提交记录。**取消本地任务不会取消平台订单、关闭实例或停止计费；强制重新开始可能产生额外实例。**

克隆只有在目标带卡运行、平台报告数据盘复制完成且源实例解除克隆锁后才成功。“运行中”或没有复制提示都不足以证明完成。不做逐文件校验。不会释放原实例，也不会自行迁移其他地区新租实例的数据。

## 页面与接口适配范围

上游与采用提交见 [THIRD_PARTY.md](THIRD_PARTY.md)。基准列表接口为 `/api/v1/instance` 和 `/api/v1/user/machine/list`，均完整分页；主账号磁盘规格读取 `GET /api/v1/instance/snapshot?instance_uuid=<完整ID>`，已捕获 `/api/v1/sub_user/instance` 列表会话的子账号使用 `GET /api/v1/sub_user/instance/snapshot?instance_uuid=<完整ID>`。这两种详情路径及参数已在用户提供的前端 reference 中核对，沿用当前列表会话的请求头和共享 Cookie，不复制列表筛选参数，也不会改用旧的主账号 token。列表会话的路径或请求头变化时，会清除缓存的详情和市场会话；其他尚未核实详情路径的列表别名会明确暂停。程序监听网页实际的请求与 JSON 响应；子账号的列表路径别名只有同时符合只读列表路径规则、成功响应、分页参数和实例/主机列表结构时才会绑定，支持已观察到的 GET/POST 方法。不会试探未知接口或把创建/开关机接口当成查询接口。查询只发往 `https://www.autodl.com`。付费操作全部通过页面点击发起，并监听该页面的提交响应。

`browser.py` 集中维护字段解析和 Python 页面流程；`assets/console.js` 是保留严格确认逻辑并调整主表定位的上游派生版本；`assets/create.js` 是允许白名单替代 GPU 的派生版本。按 reference 中实例详情面板的实际计算方式读取磁盘：系统盘为 `max_writable_size_in_byte / 1024³`，免费数据盘为 `machine_info_snapshot.max_instance_disk_size / 1024³`，付费扩容量为 `expand_data_disk_size / 1024³`。列表中的 `system_init_disk_size`、`expand_system_disk_size` 可能同时为 0，不能据此推断磁盘为 0，也不能猜测两者相加或取最大值。字节容量必须是整 GiB，单位不明或非整 GiB 时暂停，提交前仍对照实际页面复核。

解析器也兼容已有的显式 `system_disk_gb / system_disk_size`、`data_disk_gb / data_disk_size`、`data_expand_gb` 和 `expand_data_disk` 字段；系统盘新旧字段同时出现但不一致时暂停。`plan/run` 的源盘配额来自详情接口；不把免费数据盘写死为 50GB，也不把 `usage_info` 或 `root_fs_total_size` 的文件系统统计当作创建配额。`disk_expand_available` 不作为已付费扩容量。无法确定容量就暂停，不默认为零扩容。

实例 GPU 型号兼容 `snapshot_gpu_alias_name`、`gpu_name`、`gpu_type_name` 和 `snapshot_gpu_name`，优先取页面展示用的快照别名；市场 GPU 型号仍按市场字段解析。实例必要字段或源磁盘容量无法解析时，会生成 `.local/schema_diagnostics.json`，只记录字段名及类型（含有限层级的嵌套结构），不记录实例 ID、密码或其他字段值，可用于排查后续字段差异。

市场规格同时兼容上游已采用的 `cpu_per_gpu`、`mem_per_gpu`、`highest_cuda_version`、`max_instance_disk_size`，以及实际市场结构中的扩容能力字段 `max_data_disk_expand_size`（按整 GiB 字节容量读取，不能整除时暂停）。系统盘能力未返回时，保守使用默认系统盘 30GB、可扩容 0GB；不把未返回的数据盘可扩容量当作零。字段不足时使用已经核对的克隆预览主机表补全，不在市场列表页等待克隆页专用选择器。预览读取失败（包括超时）时输出 `schema_diagnostics.json`，异常原因同时保留在脱敏异常诊断中。

用户提供的 `/reference/` 含网页/HAR 原始材料，只在本地读取核对，没有加入安装包；整个目录已加入 `.gitignore`。测试只保留虚构数值与字段契约，不复制原始响应、Cookie、请求头或密码。

真实实例列表的复制状态位于 `additional_info.copy_data_disk_after_clone_status`：已核实 `Succeed` 表示完成、`Running` 表示复制中，来源 ID 为 `additional_info.src_instance_uuid`。兼容旧契约 `copy_data_disk_status`、`clone_source_uuid`，新旧字段冲突时暂停。`additional_info.clone_dst_instance` 是完成后仍可能保留的历史元数据，不作为克隆锁。源实例没有显式布尔锁字段时，只有在复制成功后，精确源实例的“克隆实例新”菜单项可操作，才以页面证据确认源实例已恢复可操作；菜单缺失或复制状态未知仍暂停。不会根据“运行中”或没有复制提示推断成功。

新租先按完整主机 ID 定位市场卡片，再打开固定主机的配置页，不使用克隆页的型号筛选器。基础镜像的 `image` 填写页面完整级联路径，例如 `PyTorch / 2.8.0 / 3.12(ubuntu22.04) / 12.8`；可选路径取决于实际页面，需核对驱动兼容性。未能读取唯一、完全一致的已选镜像时暂停。当前账号的市场、基础镜像选择和报价已完成在线只读核对，其他镜像类型仍需对应的选择控件支持。

网页只显示数据盘可扩容量而不公开系统盘可扩容量时，保守按默认系统盘 30 GB、可扩容 0 GB 筛选。系统盘需求高于已知能力的主机不会入选；新租系统盘扩容页面需要唯一可访问名称“系统盘扩容”的输入框。选择器变化时暂停，需按实际脱敏页面更新适配。

`.local/storage_state.json` 与 `.local/session_storage.json` 含登录凭据，勿分享或提交。后者补存 Playwright 的 `storage_state` 不包含的 `sessionStorage`，并与登录状态文件摘要绑定；两者不匹配时要求重新登录。恢复只作用于 AutoDL 精确来源，不覆盖标签页已刷新的会话值。数据库和 `events.jsonl` 不保存原始网页响应、SSH 密码或 token；`.local` 已加入 `.gitignore`。迁移监控环境时应整体迁移登录目录，并确保原监控进程停止。

## 测试与实机验收

0.1.6 修复主机表加载被误判为页面变化、约 1.6 秒提前结束找主机、费用更新延迟、点击前拒绝留下无法恢复的提交记录、过渡状态恢复后超时重置等问题。页面加载按 15 秒截止时间、250 毫秒间隔重读；真正的源实例、规格、价格和结构校验不会被跳过。超时或校验拒绝生成 `.local/page_diagnostics.json`，记录阶段、方法、错误码、耗时及必要的扩容容量和报价金额，不包含实例 ID、页面原文或凭据。`resume` 继续使用任务创建时的配置快照，不重新读取 `config.toml`。

库存还会检查 `on_line` 和实例数量上限：已核实前端禁止选择状态 3、4、5、6，不能仅因接口 `gpu_idle_num` 非零就视为可租。主机仍保留在库存中作为源规格参考，但不可租主机不参与候选。滚动列表出现重复主机时，准备阶段最多重新打开完整表单三轮，始终不合并或跳过重复行。详细排查与验收边界见 [DEBUG_REPORT.md](DEBUG_REPORT.md)。

安装升级后使用 `autodl-grab --version` 检查版本。可编辑安装和发行包均应为 0.1.7：`python -m pip install --no-deps -e .`。SQLite 自动兼容升级，旧待核对提交不会被清除。只有页面明确证明最终点击未发生时，操作才标记为 `aborted`；响应丢失或点击后的错误仍保留待核对记录，防止重复创建。

```powershell
python -m pytest -q
python -m compileall -q src
python -m pip install "setuptools>=68" wheel
python -m pip wheel --no-deps --no-build-isolation . -w dist
```

测试使用虚构字段样本和上游的脱敏 DOM 样本：开机状态、无卡模式、过渡状态、克隆数据仍复制、源锁、替代卡排序、整实例预算、盘容量、分页、全部无货轮询、创建响应丢失、持久化恢复、重复启动、歧义目标、登录/余额/未知错误、限流、页面变化与最终提交防重复。

`tests/test_console_dom.py` 使用 Playwright 的无头 Chromium、全新空白会话和全量拦截的虚构页面/API，默认验证慢渲染、固定列副本、页面外重复 ID、真实重复行拒绝、跨页定位及确认按钮只操作目标行。安装测试依赖和 Chromium 后即可运行；缺少 Playwright 时跳过该模块。该测试不加载项目登录状态，也不会访问真实账号或提交平台操作。

DOM 样本测试使用本地 Chrome/Edge 的全新临时无头 profile，CSP 禁止网络，检查替代型号选取、页面费用、价格变化、磁盘复制选项、结构变化及最终只点击一次。不使用你的日常浏览器会话。设置环境变量 `AUTODL_RUN_BROWSER_TESTS=1` 后启用，例如 PowerShell：`$env:AUTODL_RUN_BROWSER_TESTS="1"; python -m pytest tests/test_page_samples.py -q`。可通过 `AUTODL_TEST_BROWSER` 指定浏览器路径；缺少浏览器时跳过。受限环境可能禁止 Chromium 子进程，此时应在能正常启动浏览器的本机执行该项。JSON 样本是解析器契约样本，不宣称来自真实登录接口。

登录后的验收顺序：先执行 `plan`，核实源完整 ID、规格、盘容量、候选地区及费用；然后分别用适当预算显式运行一次原实例开机、小数据量同区克隆及其他地区新租。克隆以平台复制完成和源解锁为准；记录目标 ID 与数据状态，人工确认替代 GPU 的训练兼容性。当前仓库没有替你执行这些付费验收。
