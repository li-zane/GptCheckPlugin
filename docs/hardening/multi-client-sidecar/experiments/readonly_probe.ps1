$ErrorActionPreference = "Stop"
$ContainerName = "gptcheck-sidecar-ro-probe-$PID"
$Image = "postgres:18-alpine"
$Password = "probe-only-password"
$SetupPath = (Resolve-Path (Join-Path $PSScriptRoot "readonly_probe_setup.sql")).Path
$Checks = [ordered]@{}

function Invoke-ConnectorQuery {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $output = & docker exec `
        -e "PGPASSWORD=$Password" `
        $ContainerName `
        psql `
        -h 127.0.0.1 `
        -U gptcheck_connector `
        -d sub2api_probe `
        -A `
        -F "|" `
        -t `
        -v ON_ERROR_STOP=1 `
        -c $Sql 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousErrorActionPreference
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = ($output -join "`n").Trim()
    }
}

function Assert-Allowed {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Sql,
        [Parameter(Mandatory = $true)][string]$Expected
    )

    $result = Invoke-ConnectorQuery -Sql $Sql
    if ($result.ExitCode -ne 0 -or $result.Output -ne $Expected) {
        throw "$Name failed: exit=$($result.ExitCode), output=$($result.Output)"
    }
    $Checks[$Name] = "allowed"
}

function Assert-Denied {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Sql
    )

    $result = Invoke-ConnectorQuery -Sql $Sql
    if ($result.ExitCode -eq 0) {
        throw "$Name unexpectedly succeeded"
    }
    $Checks[$Name] = "denied"
}

try {
    $containerId = & docker run `
        --detach `
        --rm `
        --name $ContainerName `
        -e "POSTGRES_PASSWORD=$Password" `
        -v "${SetupPath}:/docker-entrypoint-initdb.d/01-setup.sql:ro" `
        $Image
    if ($LASTEXITCODE -ne 0) {
        throw "Could not start the disposable PostgreSQL container"
    }

    $ready = $false
    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    for ($attempt = 0; $attempt -lt 45; $attempt += 1) {
        $probeOutput = & docker exec `
            -e "PGPASSWORD=$Password" `
            $ContainerName `
            psql `
            -h 127.0.0.1 `
            -U postgres `
            -d sub2api_probe `
            -A `
            -t `
            -c "SELECT to_regclass('connector.api_key_inspection_inputs') IS NOT NULL" 2>$null
        if ($LASTEXITCODE -eq 0 -and ($probeOutput -join "").Trim() -eq "t") {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 500
    }
    $ErrorActionPreference = $previousErrorActionPreference
    if (-not $ready) {
        throw "PostgreSQL probe did not become ready"
    }

    Assert-Allowed `
        -Name "api_key_view_read" `
        -Sql "SELECT account_id, api_key FROM connector.api_key_inspection_inputs" `
        -Expected "1|synthetic-api-key"
    Assert-Allowed `
        -Name "oauth_view_read" `
        -Sql "SELECT account_id, access_token, refresh_token_present FROM connector.oauth_inspection_inputs" `
        -Expected "2|synthetic-access-token|t"
    Assert-Allowed `
        -Name "read_only_default" `
        -Sql "SHOW default_transaction_read_only" `
        -Expected "on"
    Assert-Allowed `
        -Name "statement_timeout" `
        -Sql "SHOW statement_timeout" `
        -Expected "3s"

    Assert-Denied `
        -Name "base_accounts_read" `
        -Sql "SELECT credentials FROM sub2api.accounts"
    Assert-Denied `
        -Name "admin_secrets_read" `
        -Sql "SELECT secret_value FROM sub2api.admin_secrets"
    Assert-Denied `
        -Name "hidden_view_column_read" `
        -Sql "SELECT refresh_token FROM connector.oauth_inspection_inputs"
    Assert-Denied `
        -Name "view_update" `
        -Sql "UPDATE connector.api_key_inspection_inputs SET api_key = 'changed' WHERE account_id = 1"
    Assert-Denied `
        -Name "base_update_after_read_only_override" `
        -Sql "SET default_transaction_read_only = off; UPDATE sub2api.accounts SET internal_note = 'changed' WHERE id = 1"
    Assert-Denied `
        -Name "schema_create" `
        -Sql "CREATE TABLE public.connector_escape (id BIGINT)"
    Assert-Denied `
        -Name "server_file_read" `
        -Sql "SELECT pg_read_file('/etc/passwd')"

    [pscustomobject]@{
        container_image = $Image
        postgres_network_published = $false
        synthetic_data_only = $true
        checks = $Checks
        result = "passed"
    } | ConvertTo-Json -Depth 4
}
finally {
    & docker stop $ContainerName *> $null
}
