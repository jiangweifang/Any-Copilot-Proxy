# Copilot Proxy

使用 **Python** 实现的 **GitHub Copilot / Microsoft 365 Copilot / Google Antigravity** 本地多渠道模型网关，提供 OpenAI Chat Completions 兼容接口。三个渠道并存共用同一本地端口：GitHub 保留原模型 ID，M365 使用 `m365/` 前缀，Antigravity 使用 `agy/` 前缀。桌面界面使用 **PySide6 / Qt**，支持 Windows、macOS 和 Linux；无界面 Linux 服务器可只安装 CLI，不需要桌面、Node.js 或 .NET。

> 非 GitHub、Microsoft 或 Google 官方项目。需要你自己的有效订阅与授权，并遵守各服务提供方、模型供应商及组织的使用政策。代理连通不代表可以使用任何地区或账号未获授权的模型。

## 快速开始

### 1. 让 AI Agent 帮你安装

复制这一句话：

```text
请按 README 帮我安装并启动 https://github.com/jiangweifang/Github-Copilot-Proxy，先确认是桌面还是无界面服务器，需要 GitHub 浏览器授权时提醒我，完成后验证服务并告诉我访问地址。
```

### 2. 或者执行安装向导

需要 **Python 3.11+**（支持 Conda）和 Git；安装器使用项目独立 `.venv`，不修改 Conda 基础环境或系统 Python。

先克隆（已有仓库则跳过）：

```text
git clone https://github.com/jiangweifang/Github-Copilot-Proxy.git
```

进入项目目录：

| 场景 | 安装入口 |
| --- | --- |
| Windows 桌面 | 双击 `install-startup.bat` |
| macOS / Linux 桌面 | `sh install.sh` |
| Linux 服务器 / SSH | `sh install.sh --headless` |
| 已激活 Conda 或指定解释器 | `python bootstrap.py`，服务器加 `--headless` |

Windows 向导依次检查手动指定的解释器、已激活的 Conda 环境、Conda 命令所在的安装目录、用户的 `.conda/environments.txt` 环境记录及常见 Conda 安装位置，最后回退到 PATH 中的 Python。支持自定义盘符和安装目录，不扫描整块磁盘；每个候选都需要满足 Python 3.11+。找不到时可运行：

```powershell
.\install-startup.ps1 -Python "C:\path\to\python.exe"
```

桌面默认中文，右上角可在 **简体中文 / English** 之间切换，选择会自动保存。安装完成后，点击 **完成设置 / 启用登录自启动**：本人完成 GitHub 设备授权，然后注册当前用户登录自启动并启动接口。需要代理时先打开 **代理设置**。无界面模式会在终端显示授权地址与设备码，可在另一台有浏览器的设备完成授权。

依赖版本由 `constraints.txt` 固定。公司网络需要镜像时，请在安装前配置 `PIP_INDEX_URL`，不要提交带凭据的源地址。例如当前环境可使用：

```powershell
$env:PIP_INDEX_URL = 'https://packagefeedproxy.microsoft.io/pypi/simple/'
```

**从旧 Node 版本升级：先退出旧托盘和服务。** 新版不会同时启动另一套占用相同端口的服务，也不会为了迁移强杀未知进程。原安装目录内的本工具用户设置保持隔离，不读取其他 Copilot 工具共享的登录缓存。

### 3. 配置 WorkBuddy

| 配置项 | 默认值 |
| --- | --- |
| 协议 | OpenAI / Chat Completions |
| Base URL | `http://127.0.0.1:4140/v1` |
| 完整请求地址（若需要） | `http://127.0.0.1:4140/v1/chat/completions` |
| API Key | 留空；客户端不允许留空时填 `unused` |
| 模型 ID | 从 `http://127.0.0.1:4140/v1/models` 返回的目录选择 |

模型目录自动获取，不需要手写账号模型列表。应用本身不会自动改写 WorkBuddy 的配置；客户端配置示例见后文。

## 桌面功能

- **语言**：默认简体中文，可即时切换 English；主窗口、已打开的代理设置、托盘和确认框同步更新。语言单独保存在用户目录的 `ui.json`，不会改动代理配置或 API 输出。
- **Dashboard**：状态、访问地址；服务停止时仅显示“启动服务”，运行时仅显示“停止服务”，停止前必须确认。其后按实际配置显示“开机自动启动”或“禁用自启动”，禁用前必须确认；这两个操作不影响当前服务，仅控制当前用户下次登录后的自动启动。
- **登录 / 退出登录（Sign in / Sign out）**：GitHub 独立设备授权；登录成功后隐藏登录按钮，只显示用户名（获取失败时显示“登录成功”）和退出登录入口，退出后再显示登录按钮。退出登录必须确认（界面默认选择“否”，CLI 默认取消），确认后停止 GitHub 渠道请求并清除其登录，保留 M365、代理设置和自启动；没有其他可用渠道时停止业务接口。再次登录系统时不自动弹 GitHub 授权。
- **Network settings**：直连、HTTP（包括 HTTPS CONNECT）、SOCKS5，支持用户名/密码与代理端 DNS。
- **监听设置 / Listener settings**：修改 `host` 和主端口 `port`，保存到 `adapter-config.json`。保存前确认；服务运行时重新监听并中断当前请求，不重启程序进程，失败则恢复旧配置和监听。停止状态只保存，不自动启动。兼容端口 `bridgePort` 和其他字段保持不变。
- **Test connection**：不保存设置，不消耗模型推理额度；未登录时测 GitHub 连通，登录后测 Copilot 权限和模型目录。
- **Save and apply**：立即切换上游连接，不重启进程；正在进行的请求可能中断，会明确返回失败，不自动重发聊天。
- **托盘**：支持的桌面环境中显示 Copilot Proxy，可打开主窗口、设置代理或退出。部分 Linux 桌面需要 AppIndicator/StatusNotifier 支持；无托盘时仍可使用主窗口。

监听与代理统一保存在根目录的 **`adapter-config.json`**；`examples` 中的文件仅供参考，应用不会自动读取。代理密码按用户选择明文保存，输入框掩码显示，状态与日志不回显；配置中填写真实密码后不要提交到 Git 或复制进镜像。

## CLI（不需要 Qt）

安装后可使用 `copilot-proxy`，或指定虚拟环境解释器：

```text
python -m copilot_proxy.cli serve --root <项目目录>
python -m copilot_proxy.cli login --root <项目目录>
python -m copilot_proxy.cli status --root <项目目录>
python -m copilot_proxy.cli stop --root <项目目录>
python -m copilot_proxy.cli start --root <项目目录>
python -m copilot_proxy.cli logout --root <项目目录>
python -m copilot_proxy.cli shutdown --root <项目目录>
python -m copilot_proxy.cli models --root <项目目录>
```

`login` 登录完成后退出，不会变成常驻服务。`serve` 才启动本程序的后台服务；未登录时不监听业务端口。`Ctrl+C` 停止前台服务。控制命令通过仅监听回环地址、带随机认证口令的私有控制通道通信，不暴露到业务 HTTP 接口。

CLI 用户可直接编辑 `adapter-config.json` 的 `host`、`port` 和 `proxy`，再执行 `stop`、`start` 加载；无需重启整个程序。兼容的 `proxy-set` / `proxy-test` 命令仍从标准输入读取 JSON，`proxy-set` 也保存到同一文件，避免把密码放入进程命令行：

```json
{"type":"socks5","host":"127.0.0.1","port":1080,"username":"","password":""}
```

桌面再次启动可运行 `.\start.ps1`（Windows）或 `sh start.sh`（macOS/Linux）；服务器用 `sh start.sh --headless`。

## 监听配置与接口

项目的 `adapter-config.json` 保持兼容：

```json
{
  "host": "127.0.0.1",
  "port": 4140,
  "bridgePort": 4141,
  "proxy": {
    "type": "none",
    "host": "",
    "port": 8080,
    "username": "",
    "password": ""
  }
}
```

- `host`：合法 IPv4；`127.0.0.1` 仅本机，`0.0.0.0` 监听所有 IPv4 网卡，也可填写本机已有的具体地址。
- `proxy.type`：`none`、`http` 或 `socks5`；`proxy.host` / `proxy.port` 为代理服务器地址与端口，账号密码可留空。这是上游代理，与程序的监听 `host` / `port` 不同。
- 旧版本用户目录里的代理设置仅在文件没有 `proxy` 字段时兼容读取；新设置以 `adapter-config.json` 为准。登录凭据和界面语言仍在私有用户目录，不写进此文件。
- 两个端口须为 `1..65535` 内不同的整数。现在由同一 Python 进程监听，不再运行 Node 桥接进程。
- `port` 提供 `/v1/models`、`/v1/chat/completions`；`bridgePort` 为兼容端口，额外提供 `/v1/responses`。客户端应连接 `port`。
- 两个渠道分别按需缓存模型目录 5 分钟；未知模型最多每 10 秒触发一次提前刷新。改变代理清除目录与路由缓存；退出某一渠道只清除该渠道的授权与目录。
- 原始模型 API 返回的 `supported_endpoints` 用于路由；缺少声明且上游明确不支持 Chat Completions 时才自动尝试 Responses。
- 可选 `models` 数组仍可按 `id` 覆盖路由属性，但不能添加账号目录之外的模型。
- 支持文本、图片、函数工具及历史、推理和 token 用量转换，以及 SSE 流。Responses 路径仅支持 `n=1`，不支持 `stop`；请求体上限 16 MiB。
- 不校验本地 API Key 或浏览器 Origin；浏览器自身 CORS/本地网络策略仍然有效。

**不要向公网或不可信网络开放端口。** 能访问业务接口的程序可以消耗账号额度。应用不修改防火墙或系统代理；TLS 验证始终启用。填 `0.0.0.0` 后，其他设备应使用服务机器的实际 IP，而不是把 `0.0.0.0` 填成访问地址。

## Docker（无界面）

镜像只安装 Python 核心，不安装 PySide6。使用同一个 `adapter-config.json`，配置文件通过只读挂载提供，登录数据保存在独立命名卷，均不会打包进镜像。

运行前把配置中的 `host` 设为 **`0.0.0.0`**，保留默认 `port: 4140` 或同步调整 Compose 的端口映射。不要与桌面版同时占用宿主机的 4140 端口。Linux 上需确保容器用户 UID `10001` 能读取挂载配置；不要为了方便而公开含密码的文件。

```text
docker compose build
docker compose run --rm proxy login --root /config
docker compose up -d
docker compose logs -f proxy
```

构建镜像时也会读取 `PIP_INDEX_URL`，公司网络可在运行 `docker compose build` 前设置前文的镜像源。这里只传不含凭据的源地址；不要将带密码的地址放入构建参数。

第二条命令只完成登录并退出；在浏览器完成设备授权后再启动服务。Compose 默认仅向宿主机 `127.0.0.1:4140` 发布主接口，不发布兼容端口或管理接口。

容器中 `proxy.host: 127.0.0.1` 指的是容器自身；如果代理运行在宿主机上，填写 `host.docker.internal` 及对应端口。宿主机代理也必须允许容器访问，应用不会自动修改它的监听或防火墙。

修改宿主机配置后重新创建容器，以确保文件挂载也更新：

```text
docker compose up -d --force-recreate
docker compose exec proxy copilot-proxy status --root /config
```

运行中重新登录可用 `docker compose exec proxy copilot-proxy login --root /config`，完成后执行同样的 `start --root /config` 命令恢复服务。未登录时业务接口不监听，健康检查会显示不健康，这是预期状态。

`docker compose down` 保留登录数据；确认不再需要账号缓存时，`docker compose down -v` 才会删除该项目的命名卷。配置中的代理密码需要在宿主机文件中另行清理。

## 自启动与干净卸载

自启动仅作用于当前用户：

- Windows：当前用户 `Run` 注册表项；升级时移除属于同一项目的旧快捷方式。
- macOS：用户 `LaunchAgents`，下次登录生效。
- Linux 桌面：XDG `autostart`。
- Linux 无界面：systemd 用户服务，需要可用的用户会话；不自动开启需要管理员授权的 linger。

退出程序不会退出 GitHub 登录；**Sign out** 才删除本工具的登录。删除本地凭据不等于在 GitHub 网站撤销 OAuth 授权。

卸载可用桌面的 **卸载（Uninstall）**、Windows 的 `uninstall.bat`、`sh uninstall.sh` 或 CLI `uninstall`。桌面先弹出清理范围确认框，默认选择“否”；取消不停止服务、不删除数据。确认后才停止本工具，移除本安装的自启动、用户配置、登录、`adapter-config.json`（含代理密码）、缓存、已知日志和旧 `node_modules`；权限或文件占用错误会明确报告，可解决后重试。

**最后退出程序，手动删除整个项目目录，包括 `.venv`、源码和 `.git`。** 运行中的解释器不能可靠地删除自身，因此不会把这一步伪装为已完成。不清除系统/Conda Python、全局 pip 缓存或其他工具的数据。

用户数据按安装目录隔离，位于：

- Windows：`%LOCALAPPDATA%\GithubCopilotProxy\<目录哈希>`
- macOS：`~/Library/Application Support/GithubCopilotProxy/<目录哈希>`
- Linux：`${XDG_CONFIG_HOME:-~/.config}/GithubCopilotProxy/<目录哈希>`

## 开发与测试

### M365 授权与聊天路由

授权链路为 **自有 Edge 扩展 → Native Messaging → 本工具私有授权存储**。Python 网关通过 BizChat WebSocket / SignalR 调用 M365，并转换成 `/v1/chat/completions` 的 JSON 或 SSE；仅登录 M365 也能启动服务。GitHub 与 M365 分别维护连接、模型目录与请求，退出其中一个渠道不会停止另一个可用渠道，不自动跨渠道重发生成请求。不安装原 M365CopilotVSCode 扩展，不使用 `51827` 或浏览器调试端口。

支持多轮文本、函数工具及结果回传、图片上传。工具调用通过受校验的文本协议转换为 OpenAI `tool_calls`，不是微软原生 function calling；工具由客户端执行，本网关不执行模型生成的命令。M365 的 `/v1/responses` 暂不支持，兼容端口的 GitHub Responses 行为保持不变。

会话识别、JWT / JWE 差异、账号场景与模型菜单结构参考 [M365CopilotVSCode](https://github.com/cristiancastineiras/M365CopilotVSCode/tree/a81c91c6509d4c020fc24ec64aaab67cba798771/packages/core/src)，Python / Native Messaging 代码独立实现。不会读取其 VS Code 凭据存储。

仅在组织允许扩展侧载和 Native Messaging 时进行验证：

1. 在项目虚拟环境中重新安装本项目以生成 `copilot-proxy-native` 入口，例如 `python -m pip install --no-deps -e .`。
2. 打开当前受管 Edge 的 `edge://extensions`，加载项目的 `browser-extension` 目录，复制该扩展的 ID。不要更改受管策略，也不要切换到干净浏览器配置来规避认证限制。
3. 使用同一个虚拟环境运行 `python -m copilot_proxy.m365_copilot.setup register --root <项目目录> --extension-id <扩展ID>`。注册只作用于当前用户；检测到其他安装或修改过的注册会拒绝覆盖。
4. 完成注册后即可使用扩展，**不必先启动聊天网关或登录 GitHub**。需要运行网关时再启动桌面程序或 `python -m copilot_proxy.cli serve --root <项目目录>`；已有旧进程需正常退出后重启，不强杀其他进程。
5. 打开本项目扩展弹窗，点击“连接此安装”。无需配对码，也不需要执行额外的连接命令。扩展与本地宿主根据安装时注册的扩展 ID 通信。
6. 在原 Edge 配置中自行登录 M365；刷新页面以捕获新会话。个人账号的加密 Token 需要实际聊天 WebSocket 的账号信息，必要时自行发送一条测试消息；这可能产生正常服务用量。
7. 运行 `python -m copilot_proxy.m365_copilot.setup status --root <项目目录>` 查看状态，网关未运行也可读取本工具已保存的授权。启动网关后，运行 `python -m copilot_proxy.m365_copilot.setup verify --root <项目目录>` 仅验证模型目录，不自动发起模型生成。真实聊天、工具及图片效果还应在目标客户端分别测试。

扩展弹窗使用中文，分别显示账号、令牌、网关和续期状态。**网关未运行不等于令牌未获取**：本地授权组件仍可保存和读取授权，聊天网关启动后读取同一份数据；组件不可达时显示“状态未知”，不冒充“未获取”。

账号一栏直接显示用户名，不再显示“工作或学校 / 个人”。过长的用户名以省略号显示，鼠标悬停可查看完整内容；上游没有提供用户名时如实提示，不从账号类型推测姓名。

主窗口提供 **启用 M365 授权同步** 开关。关闭后阻止新授权写入并暂停扩展的自动续期，保留已有登录，不影响 GitHub；扩展在下一次状态检查（最多约一分钟）显示暂停。重新打开后恢复同步。

**M365 连接设置** 可设置内部管理端口：默认 `0` 由系统选择空闲端口，扩展端默认 `0` 自动发现，因此通常不需要调整。若需要固定端口，在主程序中保存、重启本程序，再在扩展的“连接设置”里填写同一个值（也可继续用 `0` 自动发现）。该端口仅监听本机回环地址，不能与业务端口重复；保存时会检查占用，启动时仍以实际绑定结果为准。它是 Native Messaging 组件与主进程之间共享的私有管理通道，不是新增 HTTP 授权入口；浏览器扩展本身不另开监听端口。端口不一致会明确报错，不会悄悄连接其他服务。

扩展启动、连接与定时检查时，参考原项目的恢复流程，先读取当前 M365 网页的 MSAL 访问令牌索引；仅接受对应账号的 Substrate/Sydney 聊天授权，不读取刷新令牌、证书或其他扩展的缓存。加密的个人账号令牌仍需从实际聊天 WebSocket 捕获。“立即续期”先尝试读取页面已有的新授权，再刷新已有聊天页（没有则打开新页），不自动发送消息。两分钟内未收到新授权会提示手动登录，不会无限重试或显示虚假的续期成功。刷新前请先保存网页中未发送的内容。

**自动续期默认启用**：浏览器运行且此安装保持连接时，每分钟检查一次，在已知到期时间前 **12 分钟**开始尝试。先恢复网页已有的新令牌，没有则打开专用后台 M365 页面，不抢焦点或刷新用户已有的聊天页；后续只重试该后台页。单个令牌最多尝试三次，前两次间隔分别为 2、4 分钟；最后一次等待两分钟仍未获得新令牌时提示重新登录 / MFA。尝试次数与退避时间跨扩展后台进程重启保留，旧令牌不会被当作续期成功。

取得新令牌后保存授权并关闭本工具创建、且用户未接管的后台页。若用户切换到该页，扩展不再自动刷新或关闭它。断开授权后停止自动续期。浏览器关闭、组织策略要求交互、会话被撤销时不能保证续期；加密令牌无法读取到期时间时仅自动同步网页的新授权，不编造到期时间，也不承诺提前定时刷新。整个过程不自动发送聊天或消耗推理请求。

“复制令牌”在当前浏览器会话恢复了有效授权后可用，不要求聊天网关运行，复制后请勿分享；到期时间未知的个人账号授权会如实显示“有效期未知”。扩展重载后会尝试从已打开的 M365 页面恢复；网页尚未产生可读授权时会提示打开网页或重新登录。扩展 ID 收纳在“连接设置”中；“断开本地授权”不等于退出 Microsoft 网页。

弹窗中的 M365 Copilot 图标取自其 [Microsoft Store 官方页面](https://apps.microsoft.com/detail/9wzdncrd29v9)，使用该页面引用的[微软 CDN 原图](https://store-images.s-microsoft.com/image/apps.21661.9007199267161390.afb6b8cd-d194-4a99-b633-03cd80118a21.e9a094be-ee73-4e19-8cdf-49a27b0974ed)，原样随扩展提供。图标和商标属于 Microsoft，仅用于标识所连接的服务，不代表本项目由微软出品或背书。

所有命令中的 `python` 都应指向本项目 `.venv`。本地授权保存在按安装隔离、受用户权限保护的目录中，不是加密保险库；不会写入 `adapter-config.json`。网页是否能登录与 Token 能否在独立客户端使用是两回事：设备绑定、订阅或组织策略仍可能拒绝网关请求。

退出本工具的 M365 授权使用 `python -m copilot_proxy.m365_copilot.setup disconnect --root <项目目录>`。解除宿主注册使用 `python -m copilot_proxy.m365_copilot.setup unregister --root <项目目录>`；这不会退出 Microsoft 网页或删除浏览器扩展。正常项目卸载也会按归属清理宿主注册。

首版 M365 无界面 / Docker 授权支持暂缓；现有 GitHub 无界面能力不变。当前真实账号验证为 Windows 工作账号，不能据此声称其他平台及个人账号均已实测。扩展不扫描全量浏览器缓存、不导出证书、不更改代理或企业策略；浏览器关闭、授权过期和网页不续期都可能需要本人再次操作。

M365 参数边界：

- `model` 必须来自 `/v1/models`；`@paid` 明确表示订阅场景，同一个模型模式在两个场景中可能对应不同模型或额度。没有权限时返回错误，不自动降级到其他模型。
- 输入文本和工具定义合计最多 2 Mi 字符，输出适配上限为 256000 字符，同时受 16 MiB HTTP 请求体上限约束；这些是网关保护边界，不是对上游上下文或输出长度的承诺。
- 最多四张图片，每张最多 10 MiB，同时受 16 MiB 请求体上限约束。支持 PNG、JPEG、GIF、WebP、BMP 的 base64 data URL；直连时也接受公开 HTTPS 图片 URL，不跟随重定向或访问私网地址。配置上游代理时请使用 data URL。
- 支持 `tool_choice` 的 `auto`、`none`、`required`、指定函数以及 `parallel_tool_calls`；未知工具、非法参数、未完成工具输出明确失败，不伪造成功。
- `n` 只支持 1；不支持 `stop`、`logprobs`、`seed` 或严格 `response_format`。
- BizChat 不暴露原生采样器与 token 输出限制。`max_tokens`、`max_completion_tokens`、`temperature`、`top_p` 等仅作为提示传给模型，不是严格控制；会在日志与响应 `warnings` 中说明。不要依赖它们做精确长度、费用或结构化输出约束。
- 不返回编造的 token 用量或独立推理内容；WorkBuddy 示例关闭 `supportsReasoning`。

### 多渠道并存与示例配置

不增加渠道切换按钮。GitHub、M365 与 Antigravity 三个渠道共用 `http://127.0.0.1:4140/v1`，GitHub 保留原模型 ID，M365 使用 `m365/<上游模型ID>`，Antigravity 使用 `agy/<上游模型ID>`，按请求里的 `model` 区分来源，避免重名。

- [VS Code 配置示例](examples/vscode-chatLanguageModels.json)：供应商数组，`vendor: customendpoint`，包含 `M365 Copilot` 与 `Google Antigravity` 两个供应商组，每个模型使用完整 Chat Completions URL。合并到 `%APPDATA%\Code\User\chatLanguageModels.json`，不要覆盖其他供应商。
- [WorkBuddy 配置示例](examples/workbuddy-models.json)：模型数组，包含 `m365/` 与 `agy/` 常用模型示例（Antigravity 模型已开启 `supportsReasoning: true`）。合并到 `%USERPROFILE%\.workbuddy\models.json`，保留已有 GitHub / 其他渠道。

示例中使用的是实测查询到的常用模型 ID；不同订阅等级的可用列表可能不同，完整目录请直接访问 `http://127.0.0.1:4140/v1/models` 查看。`maxInputTokens: 128000` / `maxOutputTokens: 16000` 为默认客户端预算，**不是已经测得或官方保证的上下文上限**。API Key 为 `unused`，不要把浏览器令牌或其他服务的密钥填入示例。GitHub 原配置可继续保留；若也通过本项目代理 GitHub，则使用同一地址和目录中不带前缀的 ID。

开始测试时先选择 **M365 · 快速响应**，分别验证简单聊天、流式输出、工具调用后回传、图片理解。订阅模型可能有独立额度或租户限制，目录可见不保证所有模型已向账号开放。修改客户端配置后刷新模型列表或重新打开客户端。

### 合成上下文探测

模型目录不提供精确上下文上限；参考项目的 128000 / 16000 也是可配置默认值，不能代替测量。可选探测工具使用随机合成文本和五个校验记录，分别放在开头、四分之一、中间、四分之三及末尾，不读取项目或用户文件。

参考项目对范围的原始说明，见固定版本的 [Models that keep up with M365 Copilot](https://github.com/cristiancastineiras/M365CopilotVSCode/blob/a81c91c6509d4c020fc24ec64aaab67cba798771/extensiones/vscode/README.md#models-that-keep-up-with-m365-copilot) 和 [transcript.ts](https://github.com/cristiancastineiras/M365CopilotVSCode/blob/a81c91c6509d4c020fc24ec64aaab67cba798771/extensiones/vscode/src/transcript.ts)：

| 数字 | 原项目实际含义 |
| --- | --- |
| 72,000 字符 | 长请求被拒绝后采用的最低兜底预算（48,000 历史上下文 + 24,000 当前请求），不是统一的上游 token 上限。 |
| 约 448,000 字符 | 默认 128,000 输入 token 按每 token 约 3.5 字符换算的上下文预算；这只是估算。 |
| 750,000 字符 | 原项目给历史上下文部分设置的最高客户端预算；固定请求、工具说明等还有各自处理，并非上游官方限制。 |
| 446,000 字符 | 作者记载在 2026 年 11 月个人账号上，各可用模型均接受且仍能调用工具的输入量。 |
| 1,000,000 / 1,200,000 字符 | 同一记载中 Auto 接受前者、拒绝后者；不能推广到每个模型、工作账号或其他传输端点。 |
| 419,000 / 2,000,000 字符的长对话 | 该项目记载前者历史完整保留，后者只保留最后约 600,000 字符，因此长子任务会在约 400,000 字符时开启新会话。它讨论的是持续会话历史，不是单次输入的固定截断线。 |

本网关每次请求使用独立 BizChat 会话并携带客户端给出的历史，不复现原项目的自动裁剪后重试，以免跨客户端悄悄丢失上下文或重复消耗额度。客户端继续使用 128K / 16K 的参考默认预算；2 Mi 字符仅为本地请求保护界限，不表示上游一定接受。其他项目针对 StreamHub 或 Cowork 的更大上下文测试，不能直接套用到这里的 Chathub 路径。

```text
python -m pip install -c constraints.txt -e ".[benchmark]"
python -m copilot_proxy.m365_copilot.benchmark --root <项目目录> --model m365/Gpt_62_Astra_Reasoning@paid
```

默认仅打印 16K / 32K / 64K / 128K 输入的计划，**不会发送生成请求**。确认额度后加 `--run`，最多发出六次真实请求，遇到 401 / 403 / 429 立即停止。对已通过的最大输入复测两次，并报告请求是否被接受、是否找回全部校验值。

计数使用 `o200k_base` 参考 tokenizer（可用 `--encoding` 指定其他已知编码）。它不是对 M365 所选模型实际 tokenizer 的声明；微软增加的隐藏提示及服务侧处理不在计数中。通过某个长度仅证明本组测试的有效检索能力；失败可能来自截断、模型检索质量或传输限制，不能直接宣布为精确 token 上限。

### 源码分层

- `src/copilot_proxy/github_copilot/`：GitHub 设备授权、模型目录及上游客户端。
- `src/copilot_proxy/m365_copilot/`：M365 授权、BizChat 客户端、协议适配、Native Messaging 与授权管理入口。
- `src/copilot_proxy/antigravity/`：Antigravity（`agy/`）官方 `agy acp` 子进程协议桥接、`oauth-personal` 授权管理与聊天客户端。
- `chat_bridge.py`、`network.py`、`runtime.py`、`config.py`、`desktop.py`：公共文本工具协议、网络、路由与生命周期、配置和桌面界面。公共网络层不包含具体渠道授权逻辑。

### Antigravity (`agy/`) 渠道

- 通过官方 `agy` CLI 的无头 ACP 模式（`agy acp`，JSON-RPC 2.0 over `stdio`）与 `oauth-personal` Google OAuth 登录接入，直接使用个人 Google / Gemini 订阅额度，模型目录统一带 `agy/` 前缀，与 GitHub、M365 三渠道共用 `http://127.0.0.1:4140/v1`。
- 网关模式仅提供对话推理：每次会话使用空临时目录作为 `cwd`，并自动拒绝来自 `agy` 的内置文件/终端权限请求（`session/request_permission`）；外部客户端传入的 `tools` 参数通过与 M365 相同的受校验 `<tool_call>` 文本协议转换为 OpenAI `tool_calls` 由客户端自行执行。
- 支持桌面端「安装 agy CLI（实时下载与安装进度条）/ 启用 Antigravity 渠道 / 登录 Antigravity / Antigravity 设置」，以及 CLI 命令：`agy-install`、`agy-login`、`agy-logout`、`agy-status`。下载与调用均自动沿用已配置的上游 HTTP / SOCKS5 代理。

```text
python -m pip install -c constraints.txt -e ".[desktop]"
python -m unittest discover -s tests -v
```

仅安装核心用 `-e .`。测试不使用真实账号、不调用计费模型；覆盖协议转换、HTTP/SOCKS5 认证传输、代理切换、接口流式错误、登录取消、控制鉴权和归属清理。Qt 使用离屏测试，专门验证设置对话框与主窗口状态刷新不会互相覆盖。

桌面测试需要 `desktop` 可选依赖；服务器环境会跳过 Qt 测试。Linux 桌面还需对应发行版的 Qt/X11/Wayland 运行库；无界面部署不需要这些组件。

## 排错

### 运行日志

后台网关和桌面内置网关启动后都会写入本安装私有用户目录下的 `logs/gateway.log`，`copilot-proxy status` 返回的 `logFile` 字段是实际路径。每个文件最多 2 MiB，保留 3 个历史文件（`gateway.log.1` 到 `.3`），卸载本安装时一并清理。

主界面提供 **记录运行日志** 开关与 **打开日志** 按钮。开关默认开启，保存到 `adapter-config.json` 的 `logging.enabled`；运行中切换立即生效，不重启、不重新监听、不取消请求。关闭仅停止写入日志文件，保留已有文件和终端输出。首次升级到包含该开关的版本，需要正常重启程序一次以加载新代码；以后切换开关不需要重启。

日志记录启动/停止、模型目录刷新、渠道路由、HTTP 状态、请求耗时和错误，不记录聊天正文、工具参数、图片、令牌、密钥或代理密码；对常见凭据格式及 URL 查询参数另做脱敏。每个业务请求在响应头中带 `X-Request-ID`，可用它对应日志中的 `request=`。流式请求即使 HTTP 状态为 200，流中失败也会记录为 `outcome=error`，不能只看 HTTP 状态判断成功。

| 现象 | 处理 |
| --- | --- |
| 找不到 Python | 激活 Python 3.11+ 的 Conda 环境，或为安装脚本指定解释器绝对路径 |
| 端口已占用 / 旧 Node 控制记录存在 | 先退出旧版本，勿并行启动两个版本 |
| 无法访问模型目录 | 查看 Dashboard 状态，完成登录并测试代理 |
| 显示“状态未知”/无法连接 Copilot Proxy | 这不代表业务接口已停止；界面会重试连接，成功后清除旧的连接错误提示。仅识别到旧版控制记录时才提示旧 Node 版本。 |
| `401` / `403` | 检查账号、订阅和组织授权；代理不能改变服务权限 |
| 无托盘图标 | 展开隐藏图标，或检查 Linux AppIndicator 支持；主窗口仍可使用 |
| 模型仅支持 Responses | 通常按目录声明自动选择；必要时使用 `models` 路由覆盖 |
| 卸载报权限/占用错误 | 先关闭相关窗口和本项目进程，再重试；不要删除其他工具的凭据 |
