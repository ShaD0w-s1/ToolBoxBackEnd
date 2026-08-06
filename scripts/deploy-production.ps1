[CmdletBinding()]
param(
    [string]$FunctionName = "dtlapi",
    [string]$SourceRef = "main",
    [string]$RoutePath = "/api",
    [switch]$Deploy,
    [switch]$SkipSmokeTest
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$manifestPath = Join-Path $repoRoot "deploy\production-files.txt"
$envPath = Join-Path $repoRoot ".env"
$tempBase = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$stageRoot = Join-Path $tempBase ("dtl-prod-" + [Guid]::NewGuid().ToString("N"))
$functionDir = Join-Path $stageRoot $FunctionName
$archivePath = Join-Path $stageRoot "source.zip"
$script:mcpProcess = $null
$script:mcpRequestId = 10

function Read-DotEnv([string]$Path) {
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

function New-ProductionSecret {
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
            clientInfo = @{ name = "dtl-production-deploy"; version = "1.0" }
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
    if (-not $resolved.StartsWith($tempBase) -or -not $leaf.StartsWith("dtl-prod-")) {
        throw "Refusing to remove unexpected staging path: $resolved"
    }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}

function Build-ProductionPackage {
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
}

Push-Location $repoRoot
try {
    if (-not $RoutePath.StartsWith("/")) {
        throw "RoutePath must start with /"
    }
    if ($RoutePath.Length -gt 1) {
        $RoutePath = $RoutePath.TrimEnd("/")
    }

    Build-ProductionPackage

    $configuration = Read-DotEnv $envPath
    $envId = $configuration["CLOUDBASE_ENV_ID"]
    $apiKey = $configuration["CLOUDBASE_API_KEY"]
    if (-not $envId) {
        throw "CLOUDBASE_ENV_ID is missing from .env"
    }
    if (-not $apiKey -or $apiKey.StartsWith("replace-")) {
        throw "CLOUDBASE_API_KEY is missing from .env"
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

    $productionSecret = $configuration["DJANGO_PRODUCTION_SECRET_KEY"]
    if (-not $productionSecret) {
        $productionSecret = New-ProductionSecret
        Add-Content -LiteralPath $envPath -Value "`nDJANGO_PRODUCTION_SECRET_KEY=$productionSecret"
        Write-Host "Created a stable production Django secret in the ignored .env file."
    }

    $environment = @{
        DJANGO_SECRET_KEY = $productionSecret
        CLOUDBASE_ENV_ID = $envId
        CLOUDBASE_API_KEY = $apiKey
        CLOUDBASE_NOSQL_INSTANCE = "(default)"
        CLOUDBASE_NOSQL_DATABASE = "(default)"
        PYTHONUNBUFFERED = "1"
    }

    if ($existing) {
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
    if ($pathRoute -and $pathRoute.UpstreamResourceName -ne $FunctionName) {
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
