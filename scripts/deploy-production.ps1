[CmdletBinding()]
param(
    [string]$FunctionName = "toolbox-api",
    [string]$SourceRef = "main",
    [string]$RoutePath = "/api",
    [switch]$Deploy,
    [switch]$SkipSmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 所有临时文件都放在带固定前缀的系统临时目录中；清理前还会再次校验路径，
# 防止变量异常时误删仓库或其他目录。
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$manifestPath = Join-Path $repoRoot "deploy\production-files.txt"
$envPath = Join-Path $repoRoot ".env"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stageRoot = Join-Path $tempBase ("toolbox-prod-" + [Guid]::NewGuid().ToString("N"))
$functionDir = Join-Path $stageRoot $FunctionName
$archivePath = Join-Path $stageRoot "source.zip"
$script:mcpProcess = $null
$script:mcpRequestId = 10

function Read-DotEnv([string]$Path) {
    # 这里只读取部署必需的简单 KEY=VALUE，不执行 .env 中的任何代码。
    $values = @{}
    if (-not (Test-Path -LiteralPath $Path)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) {
            continue
        }
        $key = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1).Trim()
        $values[$key] = $value
    }
    return $values
}

function Get-ConfigurationValue([hashtable]$Configuration, [string]$Name) {
    # GitHub Actions 等 CI 环境直接注入变量；本地部署继续读取被忽略的 .env。
    $environmentValue = [Environment]::GetEnvironmentVariable($Name)
    if ($environmentValue) {
        return $environmentValue
    }
    return $Configuration[$Name]
}

function New-ProductionSecret {
    # 首次部署生成高熵密钥并保存到被 Git 忽略的 .env，后续部署保持不变。
    $bytes = New-Object byte[] 48
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes)
}

function Get-CloudBaseMcpCli {
    # 优先使用显式路径，否则从 npx 缓存中选择最近使用的 CloudBase MCP。
    if ($env:CLOUDBASE_MCP_CLI -and (Test-Path -LiteralPath $env:CLOUDBASE_MCP_CLI)) {
        return [IO.Path]::GetFullPath($env:CLOUDBASE_MCP_CLI)
    }

    $cacheRoot = Join-Path $env:LOCALAPPDATA "npm-cache\_npx"
    if (Test-Path -LiteralPath $cacheRoot) {
        $candidate = Get-ChildItem -LiteralPath $cacheRoot -Filter "cli.cjs" -Recurse -File |
            Where-Object { $_.FullName -like "*\node_modules\@cloudbase\cloudbase-mcp\dist\cli.cjs" } |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }

    throw "CloudBase MCP CLI not found. Run 'npx --yes @cloudbase/cloudbase-mcp@latest --help' once, or set CLOUDBASE_MCP_CLI."
}

function Start-Mcp([string]$CliPath) {
    # MCP 使用标准输入输出上的 JSON-RPC；不经过交互式 Shell，避免参数注入。
    $node = (Get-Command node.exe -ErrorAction Stop).Source
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $node
    $startInfo.Arguments = '"' + $CliPath + '"'
    $startInfo.UseShellExecute = $false
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.CreateNoWindow = $true
    $startInfo.EnvironmentVariables["INTEGRATION_IDE"] = "VSCode"

    $script:mcpProcess = New-Object System.Diagnostics.Process
    $script:mcpProcess.StartInfo = $startInfo
    [void]$script:mcpProcess.Start()

    $initialize = @{
        jsonrpc = "2.0"
        id = 1
        method = "initialize"
        params = @{
            protocolVersion = "2024-11-05"
            capabilities = @{}
            clientInfo = @{ name = "toolbox-production-deploy"; version = "1.0" }
        }
    } | ConvertTo-Json -Compress -Depth 10
    $script:mcpProcess.StandardInput.WriteLine($initialize)
    $script:mcpProcess.StandardInput.Flush()
    $response = $script:mcpProcess.StandardOutput.ReadLine() | ConvertFrom-Json
    if (-not $response.result) {
        throw "CloudBase MCP initialization failed"
    }

    $initialized = @{ jsonrpc = "2.0"; method = "notifications/initialized" } |
        ConvertTo-Json -Compress
    $script:mcpProcess.StandardInput.WriteLine($initialized)
    $script:mcpProcess.StandardInput.Flush()
}

function Invoke-McpTool([string]$Name, [hashtable]$Arguments) {
    # 每次调用使用递增请求 ID，并要求返回第一段文本是可解析 JSON。
    $script:mcpRequestId++
    $request = @{
        jsonrpc = "2.0"
        id = $script:mcpRequestId
        method = "tools/call"
        params = @{ name = $Name; arguments = $Arguments }
    } | ConvertTo-Json -Compress -Depth 30
    $script:mcpProcess.StandardInput.WriteLine($request)
    $script:mcpProcess.StandardInput.Flush()
    $response = $script:mcpProcess.StandardOutput.ReadLine() | ConvertFrom-Json
    if (-not $response.result.content -or -not $response.result.content[0].text) {
        throw "CloudBase MCP returned an invalid response for $Name"
    }
    return $response.result.content[0].text | ConvertFrom-Json
}

function Stop-Mcp {
    if ($script:mcpProcess -and -not $script:mcpProcess.HasExited) {
        $script:mcpProcess.Kill()
        $script:mcpProcess.WaitForExit()
    }
}

function Remove-StageDirectory {
    if (-not (Test-Path -LiteralPath $stageRoot)) {
        return
    }
    $resolved = [IO.Path]::GetFullPath($stageRoot)
    $leaf = [IO.Path]::GetFileName($resolved)
    if (-not $resolved.StartsWith($tempBase) -or -not $leaf.StartsWith("toolbox-prod-")) {
        throw "Refusing to remove unexpected staging path: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Build-ProductionPackage {
    # 生产包只从指定 Git 引用读取白名单文件，不直接打包工作区中的未提交内容。
    if (-not (Test-Path -LiteralPath $manifestPath)) {
        throw "Production manifest not found: $manifestPath"
    }
    $files = @(
        Get-Content -LiteralPath $manifestPath |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -and -not $_.StartsWith("#") }
    )
    if (-not $files) {
        throw "Production manifest is empty"
    }

    & git -C $repoRoot rev-parse --verify $SourceRef 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Git ref does not exist: $SourceRef"
    }
    foreach ($file in $files) {
        & git -C $repoRoot cat-file -e "${SourceRef}:$file" 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "$file is not committed in $SourceRef"
        }
    }

    New-Item -ItemType Directory -Path $functionDir -Force | Out-Null
    $gitArguments = @(
        "-C", $repoRoot, "archive", "--format=zip",
        "--output=$archivePath", $SourceRef, "--"
    ) + $files
    & git @gitArguments
    if ($LASTEXITCODE -ne 0) {
        throw "git archive failed"
    }
    Expand-Archive -LiteralPath $archivePath -DestinationPath $functionDir -Force
    Remove-Item -LiteralPath $archivePath -Force

    $bootstrap = Join-Path $functionDir "scf_bootstrap"
    if (-not (Test-Path -LiteralPath $bootstrap)) {
        throw "scf_bootstrap is missing from the production package"
    }
    if ([IO.File]::ReadAllBytes($bootstrap) -contains 13) {
        throw "scf_bootstrap must use LF line endings"
    }

    Write-Host "Production source: $SourceRef"
    Write-Host "Production files:"
    Get-ChildItem -LiteralPath $functionDir -Recurse -File |
        ForEach-Object { Write-Host ("  " + $_.FullName.Substring($functionDir.Length + 1)) }

    # CloudBase's HTTP-function updater does not reliably install Python
    # requirements, so vendor Linux CPython 3.10 wheels into the upload.
    $deploymentPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $deploymentPython -PathType Leaf)) {
        $deploymentPython = (Get-Command python.exe -ErrorAction Stop).Source
    }
    Write-Host "Vendoring Python 3.10 Linux dependencies..."
    & $deploymentPython -m pip install `
        --disable-pip-version-check `
        --no-compile `
        --platform manylinux2014_x86_64 `
        --implementation cp `
        --python-version 3.10 `
        --abi cp310 `
        --only-binary=:all: `
        --target $functionDir `
        --requirement (Join-Path $functionDir "requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to vendor production Python dependencies"
    }
}

Push-Location $repoRoot
try {
    # 第一阶段只做确定性打包和配置检查；未指定 -Deploy 时不会修改云资源。
    if (-not $RoutePath.StartsWith("/")) {
        throw "RoutePath must start with /"
    }
    if ($RoutePath.Length -gt 1) {
        $RoutePath = $RoutePath.TrimEnd("/")
    }

    Build-ProductionPackage

    $configuration = Read-DotEnv $envPath
    $envId = Get-ConfigurationValue $configuration "CLOUDBASE_ENV_ID"
    $apiKey = Get-ConfigurationValue $configuration "CLOUDBASE_API_KEY"
    if (-not $envId) {
        throw "CLOUDBASE_ENV_ID is missing from the environment and .env"
    }
    if (-not $apiKey -or $apiKey.StartsWith("replace-")) {
        throw "CLOUDBASE_API_KEY is missing from the environment and .env"
    }

    $mcpCli = Get-CloudBaseMcpCli
    Start-Mcp $mcpCli

    $auth = Invoke-McpTool "auth" @{ action = "status" }
    if ($auth.auth_status -ne "READY") {
        throw "CloudBase MCP is not logged in. Complete device authorization first."
    }
    if ($auth.current_env_id -ne $envId) {
        throw "CloudBase MCP is bound to $($auth.current_env_id), expected $envId"
    }

    $list = Invoke-McpTool "queryFunctions" @{
        action = "listFunctions"
        limit = 100
        offset = 0
    }
    if (-not $list.success) {
        throw $list.message
    }
    $existing = @($list.data.functions | Where-Object { $_.FunctionName -eq $FunctionName })

    if (-not $Deploy) {
        $operation = if ($existing) { "update" } else { "create" }
        Write-Host "Dry run complete. No cloud resources were changed."
        Write-Host "Target: $FunctionName ($operation)"
        Write-Host "Run again with -Deploy to publish this exact $SourceRef package."
        return
    }

    $productionSecret = Get-ConfigurationValue $configuration "DJANGO_PRODUCTION_SECRET_KEY"
    if (-not $productionSecret) {
        if ($env:CI) {
            throw "DJANGO_PRODUCTION_SECRET_KEY is required in CI so deployments keep a stable Django secret."
        }
        $productionSecret = New-ProductionSecret
        Add-Content -LiteralPath $envPath -Value "`nDJANGO_PRODUCTION_SECRET_KEY=$productionSecret"
        Write-Host "Created a stable production Django secret in the ignored .env file."
    }

    $csrfTrustedOrigins = Get-ConfigurationValue $configuration "CSRF_TRUSTED_ORIGINS"
    if (-not $csrfTrustedOrigins) {
        $csrfTrustedOrigins = "https://fe-da-tool-list-d2g0awsejc0658949.webapps.tcloudbase.com,https://da-tool-list-d2g0awsejc0658949-1464163374.tcloudbaseapp.com"
    }

    $environment = @{
        DJANGO_SECRET_KEY = $productionSecret
        CLOUDBASE_ENV_ID = $envId
        CLOUDBASE_API_KEY = $apiKey
        CLOUDBASE_NOSQL_INSTANCE = "(default)"
        CLOUDBASE_NOSQL_DATABASE = "(default)"
        CSRF_TRUSTED_ORIGINS = $csrfTrustedOrigins
        PYTHONUNBUFFERED = "1"
    }

    if ($existing) {
        # 更新配置时保留脚本不认识的既有环境变量，避免误删人工配置。
        $detail = Invoke-McpTool "queryFunctions" @{
            action = "getFunctionDetail"
            functionName = $FunctionName
        }
        $functionDetail = $detail.data.functionDetail
        if ($functionDetail.ProtocolType -eq "WS") {
            throw "$FunctionName is a WebSocket function. Choose a new ordinary HTTP function name."
        }
        foreach ($variable in @($functionDetail.Environment.Variables)) {
            if ($variable.Key -and -not $environment.ContainsKey($variable.Key)) {
                $environment[$variable.Key] = $variable.Value
            }
        }

        $configResult = Invoke-McpTool "manageFunctions" @{
            action = "updateFunctionConfig"
            functionName = $FunctionName
            timeout = 60
            envVariables = $environment
        }
        if (-not $configResult.success) {
            throw $configResult.message
        }
        $codeResult = Invoke-McpTool "manageFunctions" @{
            action = "updateFunctionCode"
            functionName = $FunctionName
            functionRootPath = $stageRoot
        }
        if (-not $codeResult.success) {
            throw $codeResult.message
        }
        Write-Host "Updated CloudBase function $FunctionName."
    }
    else {
        $createResult = Invoke-McpTool "manageFunctions" @{
            action = "createFunction"
            func = @{
                name = $FunctionName
                type = "HTTP"
                runtime = "Python3.10"
                timeout = 60
                envVariables = $environment
                isWaitInstall = $true
            }
            functionRootPath = $stageRoot
            force = $false
        }
        if (-not $createResult.success) {
            throw $createResult.message
        }
        Write-Host "Created CloudBase HTTP function $FunctionName."
    }

    $ready = $false
    # CloudBase 更新代码后需要异步发布，必须确认实例可用再切换网关。
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        $detail = Invoke-McpTool "queryFunctions" @{
            action = "getFunctionDetail"
            functionName = $FunctionName
        }
        $functionDetail = $detail.data.functionDetail
        if ($functionDetail.Status -eq "Active" -and $functionDetail.AvailableStatus -eq "Available") {
            $ready = $true
            break
        }
        Start-Sleep -Seconds 5
    }
    if (-not $ready) {
        throw "$FunctionName did not become available within 150 seconds"
    }

    Write-Host "Function is Active/Available."

    $privilege = Invoke-McpTool "queryGateway" @{ action = "getPrivilege" }
    if (-not $privilege.success) {
        throw $privilege.message
    }
    if (-not $privilege.data.enableService) {
        $enableResult = Invoke-McpTool "manageGateway" @{
            action = "enableService"
            enable = $true
        }
        if (-not $enableResult.success) {
            throw $enableResult.message
        }
        Write-Host "Enabled the CloudBase HTTP gateway."
    }

    $routeList = Invoke-McpTool "queryGateway" @{ action = "listRoutes" }
    if (-not $routeList.success) {
        throw $routeList.message
    }
    $pathRoute = @(
        $routeList.data.routes |
            Where-Object { $_.DomainType -eq "HTTPSERVICE" -and $_.Path -eq $RoutePath }
    )
    if ($pathRoute.Count -gt 1) {
        throw "Multiple HTTP gateway routes already use $RoutePath"
    }
    $isLegacyRoute = (
        # 仅允许已知旧名称平滑迁移；其他占用 /api 的服务仍然拒绝覆盖。
        [bool]$pathRoute -and
        $FunctionName -eq "toolbox-api" -and
        $pathRoute.UpstreamResourceName -eq "dtlapi"
    )
    if ($pathRoute -and $pathRoute.UpstreamResourceName -ne $FunctionName -and -not $isLegacyRoute) {
        throw "HTTP gateway route $RoutePath already targets $($pathRoute.UpstreamResourceName)"
    }

    if (-not $pathRoute) {
        $routeResult = Invoke-McpTool "manageGateway" @{
            action = "createRoute"
            targetName = $FunctionName
            path = $RoutePath
            upstreamResourceType = "WEB_SCF"
            auth = $false
            enablePathTransmission = $true
        }
        if (-not $routeResult.success) {
            throw $routeResult.message
        }
        Write-Host "Created public HTTP gateway route $RoutePath."
    }
    elseif (
        $pathRoute.UpstreamResourceName -ne $FunctionName -or
        $pathRoute.UpstreamResourceType -ne "WEB_SCF" -or
        $pathRoute.EnableAuth -or
        -not $pathRoute.EnablePathTransmission
    ) {
        $routeResult = Invoke-McpTool "manageGateway" @{
            action = "updateRoute"
            domain = $pathRoute.Domain
            targetName = $FunctionName
            path = $RoutePath
            upstreamResourceType = "WEB_SCF"
            auth = $false
            enablePathTransmission = $true
        }
        if (-not $routeResult.success) {
            throw $routeResult.message
        }
        Write-Host "Updated public HTTP gateway route $RoutePath."
    }

    $routeList = Invoke-McpTool "queryGateway" @{ action = "listRoutes" }
    $publicRoute = @(
        $routeList.data.routes |
            Where-Object {
                $_.DomainType -eq "HTTPSERVICE" -and
                $_.Path -eq $RoutePath -and
                $_.UpstreamResourceName -eq $FunctionName
            }
    ) | Select-Object -First 1
    if (-not $publicRoute) {
        throw "The HTTP gateway route was not visible after deployment"
    }
    $publicBaseUrl = "https://$($publicRoute.Domain)$RoutePath"
    Write-Host "Public API base URL: $publicBaseUrl"

    if (-not $SkipSmokeTest) {
        # 最终通过公开网关读取真实 NoSQL，覆盖函数、路由和凭据整条链路。
        $url = "$publicBaseUrl/projects/?limit=1"
        $response = Invoke-WebRequest -UseBasicParsing -Uri $url -Method GET -TimeoutSec 90
        if ($response.StatusCode -ne 200) {
            throw "Production smoke test returned HTTP $($response.StatusCode)"
        }
        $payload = $response.Content | ConvertFrom-Json
        if (-not $payload.ok) {
            throw "Production smoke test returned an unexpected response"
        }
        Write-Host "Smoke test passed: $url"
    }
}
finally {
    Stop-Mcp
    Remove-StageDirectory
    Pop-Location
}
