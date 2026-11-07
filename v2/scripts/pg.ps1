# Local PostgreSQL for Hamada v2 development, from the portable copy in C:\work\pgsql.
# The server is shared with the warehouse project; Hamada has its own role and database.
#   powershell -ExecutionPolicy Bypass -File v2\scripts\pg.ps1 init     # first time only
#   powershell -ExecutionPolicy Bypass -File v2\scripts\pg.ps1 start
#   powershell -ExecutionPolicy Bypass -File v2\scripts\pg.ps1 stop      # stops it for the warehouse too
#   powershell -ExecutionPolicy Bypass -File v2\scripts\pg.ps1 status
param([ValidateSet('init', 'start', 'stop', 'status')][string]$Action = 'status')

$ErrorActionPreference = 'Stop'
$PgRoot = if ($env:HAMADA_PG_ROOT) { $env:HAMADA_PG_ROOT } else { 'C:\work\pgsql' }
$Bin = Join-Path $PgRoot 'bin'
$Data = Join-Path $PgRoot 'data'
$Log = Join-Path $PgRoot 'postgres.log'
$Port = 5433
$SuperPasswordFile = Join-Path $PgRoot 'superuser-password.txt'
$EnvFile = Join-Path (Split-Path $PSScriptRoot -Parent) '.env'

function New-Secret([int]$Bytes = 24) {
    $buffer = New-Object byte[] $Bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($buffer)
    ([Convert]::ToBase64String($buffer)).Replace('+', 'A').Replace('/', 'B').TrimEnd('=')
}

function Test-Running {
    & (Join-Path $Bin 'pg_ctl.exe') status -D $Data *> $null
    return ($LASTEXITCODE -eq 0)
}

function Start-Pg {
    if (Test-Running) { Write-Output "PostgreSQL is already running on port $Port."; return }
    # Start pg_ctl through the shell so the server doesn't inherit this script's
    # output handles; otherwise a caller reading our output waits for the server.
    $arguments = "start -D `"$Data`" -l `"$Log`" -w -o `"-p $Port`""
    $process = Start-Process -FilePath (Join-Path $Bin 'pg_ctl.exe') -ArgumentList $arguments -WindowStyle Hidden -PassThru
    $process.WaitForExit()
    if (-not (Test-Running)) { throw "PostgreSQL did not start. See $Log" }
    Write-Output "PostgreSQL started on port $Port."
}

function Invoke-Psql([string]$Sql, [string]$Database = 'postgres') {
    $result = & (Join-Path $Bin 'psql.exe') -h 127.0.0.1 -p $Port -U postgres -d $Database -v ON_ERROR_STOP=1 -tAq -c $Sql
    if ($LASTEXITCODE -ne 0) { throw "psql failed: $Sql" }
    return $result
}

switch ($Action) {
    'init' {
        if (-not (Test-Path (Join-Path $Data 'PG_VERSION'))) {
            # A fresh machine: create the cluster the same way the warehouse script does.
            $superPassword = New-Secret
            $pwFile = Join-Path $env:TEMP ("pgpw-" + [guid]::NewGuid().ToString() + ".txt")
            Set-Content -Path $pwFile -Value $superPassword -NoNewline -Encoding ascii
            try {
                & (Join-Path $Bin 'initdb.exe') -D $Data -U postgres -A scram-sha-256 --pwfile=$pwFile -E UTF8 --locale=C --locale-provider=builtin --builtin-locale=C.UTF-8 | Out-Null
                if ($LASTEXITCODE -ne 0) { throw 'initdb failed' }
            }
            finally { Remove-Item $pwFile -Force -ErrorAction SilentlyContinue }
            Add-Content -Path (Join-Path $Data 'postgresql.conf') -Encoding ascii -Value @"

# development settings
listen_addresses = 'localhost'
port = $Port
"@
            Set-Content -Path $SuperPasswordFile -Value $superPassword -Encoding ascii
        }
        if (Test-Path $EnvFile) { throw "Already initialised: $EnvFile exists." }
        Start-Pg

        $env:PGPASSWORD = (Get-Content $SuperPasswordFile -Raw).Trim()
        try {
            $appPassword = New-Secret
            if (-not (Invoke-Psql "SELECT 1 FROM pg_roles WHERE rolname = 'hamada'")) {
                Invoke-Psql "CREATE ROLE hamada LOGIN CREATEDB PASSWORD '$appPassword';" | Out-Null
            }
            else {
                Invoke-Psql "ALTER ROLE hamada WITH LOGIN CREATEDB PASSWORD '$appPassword';" | Out-Null
            }
            if (-not (Invoke-Psql "SELECT 1 FROM pg_database WHERE datname = 'hamada'")) {
                Invoke-Psql "CREATE DATABASE hamada OWNER hamada;" | Out-Null
            }
            # Extensions need a superuser once; the app role only uses them.
            foreach ($db in @('hamada', 'template1')) {
                Invoke-Psql "CREATE EXTENSION IF NOT EXISTS btree_gist;" $db | Out-Null
            }
        }
        finally { Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue }

        $secretKey = (New-Secret 48)
        $lines = @(
            '# Local development only. Never commit this file.',
            'DJANGO_DEBUG=1',
            "DJANGO_SECRET_KEY=$secretKey",
            'DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost',
            'PGHOST=127.0.0.1',
            "PGPORT=$Port",
            'PGDATABASE=hamada',
            'PGUSER=hamada',
            "PGPASSWORD=$appPassword"
        )
        Set-Content -Path $EnvFile -Value $lines -Encoding utf8
        Write-Output "Database 'hamada' is ready. Settings written to $EnvFile."
    }
    'start' { Start-Pg }
    'stop' {
        if (-not (Test-Running)) { Write-Output 'PostgreSQL is not running.'; return }
        & (Join-Path $Bin 'pg_ctl.exe') stop -D $Data -m fast | Out-Null
        Write-Output 'PostgreSQL stopped (for the warehouse project too).'
    }
    'status' {
        if (Test-Running) { Write-Output "PostgreSQL is running on port $Port." } else { Write-Output 'PostgreSQL is not running.' }
    }
}
