param(
    [string]$RepoPath = (Join-Path $PSScriptRoot '..'),
    [string]$Output = (Join-Path $PSScriptRoot 'results/tmp/platform-bench'),
    [string]$ProjectPrefix = ('dev2perf-' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmss')),
    [ValidateSet('one-normal', 'two-normal', 'one-spread', 'two-spread', 'telecom-outage', 'critical-outage')]
    [string[]]$Trials = @('one-normal', 'two-normal', 'telecom-outage')
)

$ErrorActionPreference = 'Stop'
$benchRepo = (Resolve-Path -LiteralPath $RepoPath).Path
$benchOutput = (New-Item -ItemType Directory -Force -Path $Output).FullName
$benchPorts = Join-Path $PSScriptRoot 'isolated-ports.env'
if ($ProjectPrefix -notmatch '^[a-z][a-z0-9-]+$') {
    throw 'ProjectPrefix must be a new lowercase Compose project prefix.'
}

function Invoke-BenchCompose {
    param([string[]]$Arguments)
    & docker compose --project-directory $benchRepo --env-file $benchPorts -p $benchProject @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Docker Compose command failed for $benchProject ($LASTEXITCODE)."
    }
}

function Start-ProviderOutage {
    param([int]$Seconds, [string]$TrialOutput)

    $gatewayUrl = 'http://127.0.0.1:18610'
    $before = Invoke-RestMethod -TimeoutSec 5 -Uri "$gatewayUrl/messages"
    if ($before.items.Count -ne 0) {
        throw 'The outage trial requires an empty, fresh telecom receiver.'
    }
    $providerId = (Invoke-BenchCompose -Arguments @('ps', '-q', 'fake-telecom')).Trim()
    $mountFormat = '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Name}}{{end}}{{end}}'
    $volume = (& docker inspect --format $mountFormat $providerId).Trim()
    if (-not $volume) {
        throw 'The isolated gateway has no persistent receipt volume.'
    }
    Invoke-BenchCompose -Arguments @('stop', 'fake-telecom') | Out-Host
    $stoppedAt = [DateTime]::UtcNow.ToString('o')
    $stoppedTick = [System.Diagnostics.Stopwatch]::GetTimestamp()
    $containerState = (& docker inspect --format '{{.State.Status}}' $providerId).Trim()
    if ($containerState -ne 'exited') {
        throw "Expected fake-telecom to be exited. Observed $containerState."
    }
    $httpWasReachable = $false
    try {
        Invoke-RestMethod -TimeoutSec 2 -Uri "$gatewayUrl/mode" | Out-Null
        $httpWasReachable = $true
    }
    catch {
        $httpDownError = $_.Exception.Message
    }
    if ($httpWasReachable) {
        throw 'The stopped gateway was still reachable on its reserved port.'
    }
    Start-Job -ArgumentList $benchRepo, $benchPorts, $benchProject, $providerId, $volume, $mountFormat,
        $stoppedAt, $stoppedTick, $Seconds, $TrialOutput, $httpDownError, $gatewayUrl -ScriptBlock {
        param($Repo, $Ports, $Project, $ContainerId, $Volume, $MountFormat,
            $StoppedAt, $StoppedTick, $Seconds, $TrialOutput, $HttpDownError, $GatewayUrl)
        $ErrorActionPreference = 'Stop'
        $frequency = [System.Diagnostics.Stopwatch]::Frequency
        $elapsed = ([System.Diagnostics.Stopwatch]::GetTimestamp() - $StoppedTick) / $frequency
        Start-Sleep -Milliseconds ([int][Math]::Ceiling([Math]::Max(0, ($Seconds - $elapsed) * 1000)))
        $stoppedDuration = ([System.Diagnostics.Stopwatch]::GetTimestamp() - $StoppedTick) / $frequency
        if ($stoppedDuration -lt $Seconds) {
            throw 'The gateway stop interval was shorter than requested.'
        }
        $startRequested = [DateTime]::UtcNow.ToString('o')
        & docker compose --project-directory $Repo --env-file $Ports -p $Project start --wait --wait-timeout 20 fake-telecom 2>&1 |
            Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Docker Compose could not restart the fake gateway.'
        }
        $restoredId = (& docker compose --project-directory $Repo --env-file $Ports -p $Project ps -q fake-telecom).Trim()
        $restoredVolume = (& docker inspect --format $MountFormat $restoredId).Trim()
        $restoredMode = (Invoke-RestMethod -TimeoutSec 5 -Uri "$GatewayUrl/mode").mode
        if ($restoredId -ne $ContainerId -or $restoredVolume -ne $Volume -or $restoredMode -ne 'ok') {
            throw 'The original gateway and its receipt volume were not restored in ok mode.'
        }
        @{
            stopped_at_utc = $StoppedAt
            start_requested_at_utc = $startRequested
            restored_at_utc = [DateTime]::UtcNow.ToString('o')
            stopped_before_start_seconds = $stoppedDuration
            requested_seconds = $Seconds
            stopped_container_state = 'exited'
            stopped_http_error = $HttpDownError
            container_id_before = $ContainerId
            container_id_after = $restoredId
            volume_before = $Volume
            volume_after = $restoredVolume
            mode_after = $restoredMode
            receiver_items_before = 0
        } | ConvertTo-Json | Set-Content -Encoding utf8 -LiteralPath (Join-Path $TrialOutput 'outage-container.json')
    }
}

$benchTrials = @(
    @{Name = 'one-normal'; Workers = 1; Tag = 'platform-compare'; Duration = 20; NormalRate = 700; CriticalRate = 2; Outage = 0},
    @{Name = 'two-normal'; Workers = 2; Tag = 'platform-compare'; Duration = 20; NormalRate = 700; CriticalRate = 2; Outage = 0},
    @{Name = 'one-spread'; Workers = 1; NormalKeys = 256; Tag = 'platform-compare'; Duration = 20; NormalRate = 700; CriticalRate = 2; Outage = 0},
    @{Name = 'two-spread'; Workers = 2; NormalKeys = 256; Tag = 'platform-compare'; Duration = 20; NormalRate = 700; CriticalRate = 2; Outage = 0},
    @{Name = 'telecom-outage'; Workers = 1; Tag = 'platform-outage'; Duration = 6; NormalRate = 2; CriticalRate = 0; Outage = 30},
    @{Name = 'critical-outage'; Workers = 1; Tag = 'platform-critical-outage'; Duration = 6; NormalRate = 0; CriticalRate = 2; Outage = 30}
)
$benchMachine = @{
    captured_at = [DateTime]::UtcNow.ToString('o')
    source_head = (& git -C $benchRepo rev-parse HEAD)
    docker = ((& docker info --format '{{json .}}' | ConvertFrom-Json) |
        Select-Object ServerVersion, OperatingSystem, OSType, Architecture, NCPU, MemTotal, KernelVersion)
    compose_version = (& docker compose version)
    elapsed_clock = 'Host monotonic clock for container downtime; Docker VM monotonic scheduling and VM epoch delivery stamps'
}
if ($IsWindows) {
    $benchMachine['host_cpu'] = Get-CimInstance Win32_Processor |
        Select-Object Name, NumberOfCores, NumberOfLogicalProcessors
    $benchMachine['host_os'] = Get-CimInstance Win32_OperatingSystem |
        Select-Object Caption, Version, TotalVisibleMemorySize
}
$benchMachine | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $benchOutput 'machine.json')

$benchFailed = @()
foreach ($benchTrial in ($benchTrials | Where-Object { $_.Name -in $Trials })) {
    $benchProject = "$ProjectPrefix-$($benchTrial.Name)"
    $trialOutput = (New-Item -ItemType Directory -Force -Path (Join-Path $benchOutput $benchTrial.Name)).FullName
    if ((Invoke-BenchCompose -Arguments @('ps', '--all', '-q')) -or
        (& docker volume ls --filter "label=com.docker.compose.project=$benchProject" -q)) {
        throw "Project $benchProject already has containers or volumes. Choose a new prefix."
    }
    try {
        $composeTimer = [System.Diagnostics.Stopwatch]::StartNew()
        Invoke-BenchCompose -Arguments @('up', '-d', '--build', '--wait', '--wait-timeout', '90',
            '--scale', "rule-worker=$($benchTrial.Workers)")
        $composeTimer.Stop()
        $services = @(Invoke-BenchCompose -Arguments @('ps', '--format', '{{.Service}}|{{.State}}|{{.Health}}'))
        if (@($services -like 'rule-worker|running|*').Count -ne $benchTrial.Workers -or
            @($services -like 'rule-worker-critical|running|*').Count -ne 1 -or
            @($services -like 'delivery-worker|running|*').Count -ne 1) {
            throw 'Observed rule worker counts differ from the trial.'
        }
        $backendId = & docker image inspect --format '{{.Id}}' "${benchProject}-backend:local"
        $pricingUri = 'http://127.0.0.1:18617/v1/events?source=pricing-logs&status=processed&limit=1'
        $logItems = $null
        foreach ($attempt in (1..20)) {
            $logEvents = Invoke-RestMethod -TimeoutSec 5 -Uri $pricingUri
            $logItems = $logEvents.items ?? $logEvents.events
            if ($logItems) {
                break
            }
            Start-Sleep -Milliseconds 500
        }
        if (-not $logItems) {
            throw 'The demo pricing log was not visible through the frontend proxy.'
        }
        @{
            project = $benchProject
            startup_seconds = $composeTimer.Elapsed.TotalSeconds
            backend_image = $backendId
            services = $services
            logtail_event_id = $logItems[0].id
            trial = $benchTrial
        } | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $trialOutput 'stack.json')
        $providerJob = $null
        try {
            if ($benchTrial.Outage) {
                $providerJob = Start-ProviderOutage -Seconds $benchTrial.Outage -TrialOutput $trialOutput
            }
            Invoke-BenchCompose -Arguments @('run', '--no-deps', '--rm',
                '-v', "${PSScriptRoot}:/app/loadtest:ro", '-v', "$benchRepo/fixtures:/app/fixtures:ro",
                '-v', "${trialOutput}:/bench-output", 'rule-worker', 'python', '-m', 'loadtest.platform_bench',
                '--output', '/bench-output', '--tag', $benchTrial.Tag, '--duration', "$($benchTrial.Duration)",
                '--normal-rate', "$($benchTrial.NormalRate)", '--critical-rate', "$($benchTrial.CriticalRate)",
                '--normal-workers', "$($benchTrial.Workers)", '--normal-keys', [string]($benchTrial.NormalKeys ?? 1),
                '--stopped-provider-seconds', "$($benchTrial.Outage)")
        }
        finally {
            if ($providerJob) {
                try {
                    if (-not (Wait-Job -Job $providerJob -Timeout 45)) {
                        throw 'Timed out waiting for the isolated gateway to restart.'
                    }
                    Receive-Job -Job $providerJob
                    if (-not (Test-Path -LiteralPath (Join-Path $trialOutput 'outage-container.json'))) {
                        throw 'No completed gateway stop/start record was written.'
                    }
                }
                finally {
                    Remove-Job -Job $providerJob -Force
                }
            }
        }
        Invoke-RestMethod -TimeoutSec 5 -Uri 'http://127.0.0.1:18617/v1/stats/funnel' |
            ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 -LiteralPath (Join-Path $trialOutput 'funnel.json')
        (Invoke-WebRequest -TimeoutSec 5 -Uri 'http://127.0.0.1:18600/metrics').Content |
            Set-Content -Encoding utf8 -LiteralPath (Join-Path $trialOutput 'metrics.prom')
    }
    catch {
        $benchFailed += $benchTrial.Name
        Write-Warning $_
    }
    finally {
        $rawPath = Join-Path $trialOutput 'raw.json.gz'
        if (Test-Path -LiteralPath $rawPath) {
            $rawHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $rawPath).Hash.ToLowerInvariant()
            "$rawHash  raw.json.gz" | Set-Content -Encoding ascii -LiteralPath "$rawPath.sha256"
        }
        Invoke-BenchCompose -Arguments @('down')
    }
}
if ($benchFailed) {
    throw "Some trials failed ($($benchFailed -join ', ')). View the available evidence in $benchOutput."
}
foreach ($shape in 'normal','spread') {
    if ("one-$shape" -notin $Trials -or "two-$shape" -notin $Trials) { continue }
    $one = Get-Content -Raw -LiteralPath (Join-Path $benchOutput "one-$shape/summary.json") | ConvertFrom-Json
    $two = Get-Content -Raw -LiteralPath (Join-Path $benchOutput "two-$shape/summary.json") | ConvertFrom-Json
    if ($one.workload_sha256 -ne $two.workload_sha256) {
        throw 'The worker trials used different input schedules.'
    }
}
