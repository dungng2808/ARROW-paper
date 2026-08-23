param(
  [int[]]$Versions = @(8, 11, 17, 21),
  [string]$Destination = "",
  [switch]$Force
)

$ErrorActionPreference = "Stop"

function Write-Step {
  param([string]$Message)
  Write-Host ""
  Write-Host "==> $Message" -ForegroundColor Cyan
}

function Resolve-ArrowRoot {
  if (-not $PSCommandPath) {
    return (Get-Location).Path
  }
  return (Resolve-Path (Join-Path (Split-Path -Parent $PSCommandPath) "..")).Path
}

function Resolve-AzulArchitecture {
  $architecture = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
  switch ($architecture) {
    "X64" { return "x64" }
    "Arm64" { return "arm" }
    default { throw "Unsupported Windows architecture: $architecture" }
  }
}

function Test-JavaHome {
  param([string]$Path)
  return (Test-Path -LiteralPath (Join-Path $Path "bin\java.exe"))
}

function Get-ZuluRelease {
  param(
    [int]$Version,
    [string]$Architecture
  )

  $query = @(
    "java_version=$Version",
    "os=windows",
    "arch=$Architecture",
    "archive_type=zip",
    "java_package_type=jdk",
    "javafx_bundled=false",
    "release_status=ga",
    "availability_types=ca",
    "latest=true",
    "include_fields=sha256_hash",
    "page=1",
    "page_size=1"
  ) -join "&"
  $uri = "https://api.azul.com/metadata/v1/zulu/packages/?$query"
  $response = @(Invoke-RestMethod -Uri $uri -Method Get)
  if ($response.Count -lt 1) {
    throw "No Azul Zulu JDK $Version package found for Windows/$Architecture"
  }
  $release = $response[0]
  if (-not $release.download_url -or -not $release.sha256_hash -or -not $release.name) {
    throw "Azul metadata is incomplete for JDK $Version"
  }
  return $release
}

function Get-FileSha256 {
  param([string]$Path)
  return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
}

function Assert-SafeTarget {
  param(
    [string]$Target,
    [string]$Root
  )
  $rootPrefix = [System.IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
  $fullTarget = [System.IO.Path]::GetFullPath($Target)
  if (-not $fullTarget.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to modify path outside Java-version: $fullTarget"
  }
}

function Install-JdkVersion {
  param(
    [int]$Version,
    [string]$Root,
    [string]$Cache,
    [string]$Architecture
  )

  $target = Join-Path $Root "java-$Version"
  Assert-SafeTarget -Target $target -Root $Root
  if ((Test-JavaHome $target) -and -not $Force) {
    Write-Host "JDK $Version already exists: $target" -ForegroundColor Green
    return $target
  }

  $release = Get-ZuluRelease -Version $Version -Architecture $Architecture
  $archive = Join-Path $Cache $release.name
  $expectedHash = $release.sha256_hash.ToLowerInvariant()

  $needsDownload = $true
  if (Test-Path -LiteralPath $archive) {
    $actualHash = Get-FileSha256 -Path $archive
    if ($actualHash -eq $expectedHash) {
      Write-Host "Using verified cached archive: $archive" -ForegroundColor DarkGray
      $needsDownload = $false
    } else {
      Write-Warning "Cached archive checksum mismatch; downloading it again."
      Remove-Item -LiteralPath $archive -Force
    }
  }

  if ($needsDownload) {
    Write-Step "Downloading Azul Zulu JDK $Version for Windows/$Architecture"
    $partial = "$archive.part"
    if (Test-Path -LiteralPath $partial) {
      Remove-Item -LiteralPath $partial -Force
    }
    try {
      Invoke-WebRequest -Uri $release.download_url -OutFile $partial -MaximumRedirection 10
      if ((Get-FileSha256 -Path $partial) -ne $expectedHash) {
        throw "SHA256 mismatch for downloaded JDK $Version"
      }
      Move-Item -LiteralPath $partial -Destination $archive -Force
    } finally {
      if (Test-Path -LiteralPath $partial) {
        Remove-Item -LiteralPath $partial -Force
      }
    }
  }

  $extractRoot = Join-Path $Cache "extract-$Version"
  Assert-SafeTarget -Target $extractRoot -Root $Root
  if (Test-Path -LiteralPath $extractRoot) {
    Remove-Item -LiteralPath $extractRoot -Recurse -Force
  }
  New-Item -ItemType Directory -Path $extractRoot | Out-Null

  Write-Step "Extracting JDK $Version"
  Expand-Archive -LiteralPath $archive -DestinationPath $extractRoot -Force
  $javaExecutable = Get-ChildItem -LiteralPath $extractRoot -File -Filter "java.exe" -Recurse |
    Where-Object { $_.Directory.Name -eq "bin" } |
    Select-Object -First 1
  if (-not $javaExecutable) {
    throw "Cannot find bin\java.exe after extracting $archive"
  }
  $javaHome = $javaExecutable.Directory.Parent.FullName

  if (Test-Path -LiteralPath $target) {
    if (-not $Force) {
      throw "Target exists but is not a valid JDK; rerun with -Force: $target"
    }
    Remove-Item -LiteralPath $target -Recurse -Force
  }
  Move-Item -LiteralPath $javaHome -Destination $target
  Remove-Item -LiteralPath $extractRoot -Recurse -Force

  if (-not (Test-JavaHome $target)) {
    throw "Installed JDK $Version but bin\java.exe is missing: $target"
  }
  Write-Host "Installed JDK $Version -> $target" -ForegroundColor Green
  return $target
}

$arrowRoot = Resolve-ArrowRoot
if (-not $Destination) {
  $Destination = Join-Path $arrowRoot "Java-version"
}
$Destination = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Destination)
$cacheDir = Join-Path $Destination ".cache"
$architecture = Resolve-AzulArchitecture

Write-Step "Preparing local Java-version directory"
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
New-Item -ItemType Directory -Path $cacheDir -Force | Out-Null
Write-Host "Destination: $Destination"
Write-Host "Architecture: $architecture"

[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$installed = @{}
foreach ($version in $Versions) {
  if ($version -notin @(8, 11, 17, 21)) {
    throw "Unsupported requested Java version: $version"
  }
  $installed[$version] = Install-JdkVersion `
    -Version $version `
    -Root $Destination `
    -Cache $cacheDir `
    -Architecture $architecture
}

$mapPath = Join-Path $Destination "java-version-map.txt"
$mapLines = foreach ($version in ($installed.Keys | Sort-Object)) {
  "java-$version`: $($installed[$version])"
}
$mapLines | Set-Content -LiteralPath $mapPath -Encoding UTF8

$activationPath = Join-Path $Destination "activate-java-versions.ps1"
$activationLines = @('$env:JAVA_VERSIONS_HOME = $PSScriptRoot')
foreach ($version in ($installed.Keys | Sort-Object)) {
  $activationLines += "`$env:JAVA_${version}_HOME = Join-Path `$PSScriptRoot 'java-$version'"
}
$activationLines | Set-Content -LiteralPath $activationPath -Encoding UTF8

. $activationPath
Write-Step "Verifying installed Java versions"
foreach ($version in ($installed.Keys | Sort-Object)) {
  $javaExecutable = Join-Path $installed[$version] "bin\java.exe"
  Write-Host ""
  Write-Host "java-$version -> $($installed[$version])" -ForegroundColor Yellow
  & $javaExecutable -version
  & (Join-Path $installed[$version] "bin\javac.exe") -version
}

Write-Host ""
Write-Host "Installation completed." -ForegroundColor Green
Write-Host "For each new PowerShell window, run:" -ForegroundColor Green
Write-Host ". .\Java-version\activate-java-versions.ps1" -ForegroundColor Yellow
