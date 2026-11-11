# ================================================================
# Raccoon Shorts - Completion Notification Script
# Shows Windows notification when video generation completes
# ================================================================

param(
    [string]$message = "Raccoon Shorts video generation completed!",
    [string]$title = "Raccoon Shorts"
)

Add-Type -AssemblyName System.Windows.Forms

$balloon = New-Object System.Windows.Forms.NotifyIcon
$balloon.Icon = [System.Drawing.SystemIcons]::Information
$balloon.BalloonTipTitle = $title
$balloon.BalloonTipText = $message
$balloon.BalloonTipIcon = "Info"
$balloon.Visible = $true
$balloon.ShowBalloonTip(10000)

# Wait for notification to show before script exits
Start-Sleep -Seconds 10

$balloon.Dispose()