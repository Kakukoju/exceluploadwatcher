# ============================================================
# Tutti QC AssayProcess Local Config
# ============================================================

$WatchRoots = @(
    '\\fls341\MBBU_FAB\MB_QA\Disc 首件檢查\Tutti\1. 生化盤'
    '\\fls341\MBBU_FAB\MB_QA\Disc 首件檢查\Tutti\2. 比濁盤'
    '\\fls341\MBBU_FAB\MB_QA\Disc 首件檢查\Tutti\3. ELISA盤'
    '\\fls341\MBBU_FAB\MB_QA\Disc 首件檢查\Tutti\4. 凝血盤'
)

$BaseRoot = $WatchRoots[0]
$FullUploadRoot = $BaseRoot

$WatcherUseCurrentMonthFolder = $true
$WatcherTargetMonthFolder = ''

$FilePattern = 'AssayProcess_*.csv'
$MachineFileRegex = '^AssayProcess_\d{14}\.csv$'

$UploadUrl = 'https://52-192-28-39.sslip.io/api/assayprocess/upload-assay-process-csv'

$PollSeconds = 60
$FullScanIntervalMinutes = 30
$RecentWindowMinutes = 10
$ManifestPath = '.\upload_manifest.json'