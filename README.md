# GitHub Copilot Proxy

为 WorkBuddy 等支持 OpenAI Chat Completions 协议的桌面客户端提供本地 GitHub Copilot 接口。

本项目在 [copilot-bridge](https://github.com/albanx/copilot-proxy) 前增加一层适配：校验本地 API Key、限制可用模型，并将支持 Responses API 的模型输出转换为 Chat Completions 格式。

> **非 GitHub 官方项目。** 需要你自己的有效 Copilot 订阅和使用权限，不提供共享账号、GitHub Token 或免费额度。请遵守 [GitHub 使用政策](https://docs.github.com/site-policy/acceptable-use-policies/github-acceptable-use-policies)及 [Copilot 条款](https://docs.github.com/site-policy/github-terms/github-terms-for-additional-products-and-features#github-copilot)，并确认组织允许此类客户端。上游接口可能随时变化。

## 功能与边界

- `GET /v1/models`：返回本地配置中的模型列表。
- `POST /v1/chat/completions`：支持普通响应与 SSE 流式响应。
- 保留文本、图片、函数工具调用与工具结果历史；Responses 路径支持推理内容、结构化输出与 token 用量转换。
- 根据本地模型配置的 `supported_endpoints` 选择 Responses 或 Chat Completions 上游接口。
- 仅监听 `127.0.0.1`，校验 Bearer Key，限制浏览器 Origin 和 16 MiB 请求体。
- 不在适配器端暴露 `/v1/responses`、`/v1/messages`、`/v1/embeddings`，也不承诺完整兼容所有 OpenAI 参数。Responses 路径仅支持 `n=1`，不支持 `stop`。

```text
WorkBuddy / OpenAI-compatible client
    |
    | http://127.0.0.1:4140/v1 + local API Key
    v
adapter.mjs
    |
    | http://127.0.0.1:4141/v1
    v
copilot-bridge -> GitHub Copilot
```

## 环境要求

- Windows 10/11、Windows PowerShell 5.1。
- Node.js 22 或更高版本、npm、Git。
- 有效的 GitHub Copilot 订阅，以及可以访问 GitHub / Copilot 服务的网络。
- 本机 `4140`、`4141` 端口未被其他程序占用。

以下命令在 PowerShell 中执行。启动脚本目前面向 Windows；其他平台需要自行管理两个 Node.js 进程。

## 快速开始

### 1. 下载并安装

```powershell
git clone https://github.com/jiangweifang/Github-Copilot-Proxy.git
Set-Location .\Github-Copilot-Proxy
npm ci
```

依赖版本由 `package-lock.json` 锁定。如果公司网络限制公共 npm，请按所在组织的规范配置自己的 npm 镜像；不要把带凭据的 `.npmrc` 提交到仓库。

### 2. 登录 GitHub Copilot

```powershell
npm run auth
```

按终端提示完成浏览器设备授权。GitHub 登录凭据由 `copilot-bridge` 管理，不要填入本项目的配置或 README。依赖的默认凭据缓存位于用户目录下的 `.local\share\copilot-api\github_token`，不要复制或上传此文件。

### 3. 生成仅保存在本机的配置

仓库只提供空的 `adapter-config.example.json`，不包含真实 API Key 或账号模型目录。不要直接使用空模板启动。

先在**终端 A**（项目目录）启动临时桥接服务，等待登录及初始化完成：

```powershell
npx --no-install copilot-bridge start --host 127.0.0.1 --port 4141 --rate-limit 3 --wait
```

再在**终端 B**（同一项目目录）执行以下代码。它会从本机桥接服务读取你账号的模型目录、生成随机本地 Key，并以无 BOM 的 UTF-8 写入配置；不会在终端输出 Key，也不会覆盖已有配置：

```powershell
$ErrorActionPreference = 'Stop'
if (Test-Path -LiteralPath .\adapter-config.json) {
    throw 'adapter-config.json already exists. Keep it, or move it to a private backup before reconfiguring.'
}
$catalog = Invoke-RestMethod -Uri 'http://127.0.0.1:4141/v1/models' -TimeoutSec 30
if (@($catalog.data).Count -eq 0) {
    throw 'The upstream model catalog is empty.'
}
$config = Get-Content -LiteralPath .\adapter-config.example.json -Raw | ConvertFrom-Json
$config.apiKey = node -e "process.stdout.write(require('node:crypto').randomBytes(32).toString('hex'))"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($config.apiKey)) {
    throw 'Failed to generate the local API key.'
}
$config.models = @($catalog.data)
$path = Join-Path (Get-Location).Path 'adapter-config.json'
[IO.File]::WriteAllText($path, ($config | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
Write-Host 'Local configuration created. Keep adapter-config.json private.'
```

`apiKey` 是客户端访问 **4140 端口**所需的随机本地口令，**不是 GitHub Token**。`models` 使用你账号返回的 `data` 数组，不要从其他账号复制模型清单。

**重要：配置 Responses 模型的路由。** `copilot-bridge` 2.1.0 的模型列表不返回原始 `supported_endpoints`，因此上述步骤只能发现模型 ID，不能自动判断调用协议。没有该字段的模型默认走 Chat Completions。对于需要 Responses API 的模型，请根据该模型的接口要求，在本地配置的对应模型对象中补充：

```json
{
  "id": "YOUR_RESPONSES_MODEL_ID",
  "supported_endpoints": ["/responses"]
}
```

将 `YOUR_RESPONSES_MODEL_ID` 换成列表中的真实 ID；这是配置片段，不要覆盖外层的 `apiKey` 和 `models`。模型支持两种接口时，配置包含 `/responses` 就优先走 Responses。不要给所有模型统一设置此字段，也不要仅凭模型名字猜测接口。若无法确认，先选用账号中明确支持 Chat Completions 的模型；上游提示仅支持 Responses 时，再为该模型设置该字段。

回到**终端 A**，按 `Ctrl+C` 停止临时桥接服务，然后继续下一步。

### 4. 启动

```powershell
npm start
```

启动脚本同时管理桥接服务和适配器；保持此终端打开，使用 `Ctrl+C` 停止。它不会关闭其他已占用端口的程序。如果任一子进程退出，脚本会报错并清理自己启动的子进程。

日志保存在项目目录中的 `proxy.log`、`proxy-error.log`、`adapter.log`、`adapter-error.log`，均被 Git 忽略。桥接服务完成初始化前，请等待片刻再发送请求。

### 5. 配置 WorkBuddy

在 WorkBuddy 的自定义模型 / OpenAI 兼容提供商设置中填写（具体界面名称以客户端版本为准）：

| 配置项 | 值 |
| --- | --- |
| 接口类型 | OpenAI / Chat Completions |
| Base URL | `http://127.0.0.1:4140/v1` |
| API Key | 本机 `adapter-config.json` 中的 `apiKey` |
| 模型 ID | `/v1/models` 返回的一个确切 ID |

不要把 Base URL 指向 `4141`，否则会绕过本项目的本地 Key 校验及适配层。不要把本地 Key 填成 GitHub Token。复制 Key 时注意剪贴板、录屏及截图安全。

## 验证接口

保持 `npm start` 运行，在另一个 PowerShell 终端执行：

```powershell
$config = Get-Content -LiteralPath .\adapter-config.json -Raw | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($config.apiKey)" }
$models = Invoke-RestMethod -Uri 'http://127.0.0.1:4140/v1/models' -Headers $headers
$models.data | Select-Object id

# Enter an available chat model from the list; this request consumes account quota.
$modelId = Read-Host 'Chat model ID'
$body = @{
    model = $modelId
    messages = @(@{ role = 'user'; content = 'Hello' })
    stream = $false
} | ConvertTo-Json -Depth 10
Invoke-RestMethod -Uri 'http://127.0.0.1:4140/v1/chat/completions' `
    -Method Post -Headers $headers -ContentType 'application/json' -Body $body -TimeoutSec 300
```

流式客户端使用同一接口并传入 `"stream": true`，响应为 SSE，结束标记为 `data: [DONE]`。

## 更新模型目录

账号权限或 Copilot 模型发生变化后，在服务运行时重新获取目录，保留原本的本地 Key 和已有的路由字段：

```powershell
$ErrorActionPreference = 'Stop'
$catalog = Invoke-RestMethod -Uri 'http://127.0.0.1:4141/v1/models' -TimeoutSec 30
if (@($catalog.data).Count -eq 0) { throw 'The upstream model catalog is empty.' }
$config = Get-Content -LiteralPath .\adapter-config.json -Raw | ConvertFrom-Json
$routes = @{}
foreach ($model in $config.models) {
    if ($model.supported_endpoints) {
        $routes[$model.id] = @($model.supported_endpoints)
    }
}
foreach ($model in $catalog.data) {
    if ($routes.ContainsKey($model.id)) {
        $model | Add-Member -NotePropertyName supported_endpoints -NotePropertyValue $routes[$model.id] -Force
    }
}
$config.models = @($catalog.data)
$path = Join-Path (Get-Location).Path 'adapter-config.json'
[IO.File]::WriteAllText($path, ($config | ConvertTo-Json -Depth 100), [Text.UTF8Encoding]::new($false))
```

新出现的 Responses 模型仍需按首次配置步骤补充路由字段。完成后停止并重新执行 `npm start`。配置在启动时加载，不会自动热更新。

## 安全与隐私

- 仓库不包含真实密钥、登录缓存、私有模型清单、本机配置或运行日志。`.gitignore` 防止这些常见文件被误提交，但不能替代提交前检查；不要使用 `git add -f` 添加敏感文件。
- `adapter-config.json` 以明文保存本地 Key，请限制该文件的访问权限；怀疑泄露时重新生成 Key 并更新客户端配置。
- **不要把两个端口映射到公网或共享网络。** `copilot-bridge` 的 4141 端口本身没有入站身份认证，本机其他程序仍可能直接访问它。4140 的 Key 不能保护 4141。
- 不要使用桥接服务的 `--show-token` 选项，不要公开原始日志；日志、错误信息和模型目录也可能包含账号相关数据。
- 请求内容会经桥接服务发送给 GitHub Copilot。请遵守账号与组织的数据使用要求，不要提交不允许外发的内容。
- 源码中的 `unused-local-bridge` 只是 OpenAI SDK 要求的非敏感占位字符串，不是登录凭据；真正的 GitHub 授权由桥接服务处理。

## 测试与项目结构

```powershell
npm test
```

测试无需 GitHub 登录，也不会调用 Copilot。覆盖协议转换、工具调用、流式错误及空密钥 / 无效模型配置的启动拒绝行为；不替代真实账号的端到端验证。

| 文件 | 用途 |
| --- | --- |
| `adapter.mjs` | 本地鉴权、模型路由、HTTP / SSE 适配 |
| `protocol.mjs` | Chat Completions 与 Responses 转换 |
| `protocol.test.mjs` | 协议转换回归测试 |
| `adapter.test.mjs` | 无效本地配置启动检查 |
| `start.ps1` | Windows 双进程启动和清理 |
| `adapter-config.example.json` | 无密钥配置模板 |
| `package-lock.json` | 可复现的依赖版本 |

## 常见问题

| 现象 | 排查 |
| --- | --- |
| 缺少 `adapter-config.json` / 配置为空 | 完成首次配置步骤，不要直接运行空模板。 |
| `401 Local API key required` | 客户端使用本地 `apiKey`，请求头必须是 `Authorization: Bearer ...`。 |
| 模型不在目录中 | 使用确切 ID，刷新目录后重启适配器；账号权限以 GitHub 为准。 |
| 模型要求 Responses / 不支持 Chat Completions | 在对应模型配置中加入 `"supported_endpoints": ["/responses"]` 并重启；不要对所有模型盲目设置。 |
| `4140` / `4141` 已被占用 | 先自行确认并停止旧服务；脚本不会杀掉未知进程。 |
| 上游 `401` / `403` | 重新执行 `npm run auth`，检查订阅、组织策略和授权状态。 |
| 上游 `429` | 降低调用频率，遵守订阅限额；默认请求间隔为 3 秒。 |
| 连接失败 / `502` / 流中断 | 检查桥接初始化、网络和本机错误日志；上传日志前先脱敏。 |

## 依赖与声明

本项目依赖 `copilot-bridge` 和官方 `openai` JavaScript SDK；依赖的版权与许可归各自作者所有。本仓库不包含这些依赖的源码副本，使用前请检查各依赖的适用条款。
