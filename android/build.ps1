param([string]$Task = ':app:assembleDebug', [switch]$Qa)
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$toolDir = Join-Path $projectDir '.tools'
New-Item -ItemType Directory -Force -Path $toolDir | Out-Null
$gradleZip = Join-Path $toolDir 'gradle-8.13-bin.zip'
$gradleCmd = Join-Path $toolDir 'gradle-8.13\bin\gradle.bat'
if (-not (Test-Path -LiteralPath $gradleCmd)) {
    Invoke-WebRequest 'https://services.gradle.org/distributions/gradle-8.13-bin.zip.sha256' -OutFile "$gradleZip.sha256"
    if (-not (Test-Path -LiteralPath $gradleZip)) {
        Invoke-WebRequest 'https://services.gradle.org/distributions/gradle-8.13-bin.zip' -OutFile $gradleZip
    }
    $expected = (Get-Content -LiteralPath "$gradleZip.sha256" -Raw).Trim()
    if ((Get-FileHash -LiteralPath $gradleZip -Algorithm SHA256).Hash -ne $expected) { throw 'Gradle checksum mismatch' }
    Expand-Archive -LiteralPath $gradleZip -DestinationPath $toolDir -Force
}
if (-not $env:JAVA_HOME) {
    $java = Get-Command java -ErrorAction SilentlyContinue
    if ($java -and $java.Source.EndsWith('java.exe')) { $env:JAVA_HOME = Split-Path -Parent (Split-Path -Parent $java.Source) }
}
if (-not $env:JAVA_HOME) {
    $adoptium = Join-Path $env:ProgramFiles 'Eclipse Adoptium'
    $jdk = Get-ChildItem -LiteralPath $adoptium -Directory -Filter 'jdk-21*' -ErrorAction SilentlyContinue |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName 'bin\java.exe') } |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($jdk) { $env:JAVA_HOME = $jdk.FullName }
}
if (-not $env:JAVA_HOME) { throw 'JDK 21 não encontrado. Defina JAVA_HOME antes de compilar.' }
if (-not $env:ANDROID_HOME) { $env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA 'Android\Sdk' }
if (-not $env:GRADLE_USER_HOME) { $env:GRADLE_USER_HOME = Join-Path $projectDir '.gradle-user' }
if (-not $env:ANDROID_USER_HOME) { $env:ANDROID_USER_HOME = Join-Path $projectDir '.android' }
$taskArgs = @('-p', $projectDir, $Task, '--console=plain')
if ($Qa) { $taskArgs += '-Pqa' }
& $gradleCmd @taskArgs
exit $LASTEXITCODE
