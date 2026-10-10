param([Parameter(Mandatory=$true)][string]$CredentialPath)
$ErrorActionPreference='Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Security
$form=New-Object Windows.Forms.Form
$form.Text='A_System：QMT 自动登录凭据'
$form.Size=New-Object Drawing.Size(510,280)
$form.StartPosition='CenterScreen'
$form.TopMost=$true
$form.FormBorderStyle='FixedDialog'
$form.MaximizeBox=$false
$label=New-Object Windows.Forms.Label
$label.Text='仅在这台 Windows、当前用户下加密保存，用于登录国金 QMT。密码不会发送到聊天、日志或同步目录。'
$label.Location=New-Object Drawing.Point(20,15)
$label.Size=New-Object Drawing.Size(450,45)
$form.Controls.Add($label)
$accountLabel=New-Object Windows.Forms.Label
$accountLabel.Text='资金账号'
$accountLabel.Location=New-Object Drawing.Point(20,78)
$form.Controls.Add($accountLabel)
$account=New-Object Windows.Forms.TextBox
$account.Location=New-Object Drawing.Point(125,75)
$account.Width=335
$form.Controls.Add($account)
$passwordLabel=New-Object Windows.Forms.Label
$passwordLabel.Text='交易密码'
$passwordLabel.Location=New-Object Drawing.Point(20,118)
$form.Controls.Add($passwordLabel)
$password=New-Object Windows.Forms.TextBox
$password.UseSystemPasswordChar=$true
$password.Location=New-Object Drawing.Point(125,115)
$password.Width=335
$form.Controls.Add($password)
$save=New-Object Windows.Forms.Button
$save.Text='加密保存'
$save.Location=New-Object Drawing.Point(240,175)
$save.Width=105
$form.Controls.Add($save)
$cancel=New-Object Windows.Forms.Button
$cancel.Text='取消'
$cancel.Location=New-Object Drawing.Point(355,175)
$cancel.Width=105
$cancel.DialogResult=[Windows.Forms.DialogResult]::Cancel
$form.Controls.Add($cancel)
$form.CancelButton=$cancel
$save.Add_Click({
    if($account.Text -notmatch '^\d{6,20}$' -or [string]::IsNullOrWhiteSpace($password.Text)){
        [void][Windows.Forms.MessageBox]::Show('请填写资金账号和交易密码。','未保存')
        return
    }
    $plain=@{account=$account.Text;password=$password.Text} | ConvertTo-Json -Compress
    $bytes=[Text.Encoding]::UTF8.GetBytes($plain)
    $encrypted=[Security.Cryptography.ProtectedData]::Protect($bytes,$null,[Security.Cryptography.DataProtectionScope]::CurrentUser)
    $document=@{credentials_dpapi_b64=[Convert]::ToBase64String($encrypted);format_version=1} | ConvertTo-Json
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($CredentialPath)) | Out-Null
    [IO.File]::WriteAllText($CredentialPath+'.tmp',$document,[Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath ($CredentialPath+'.tmp') -Destination $CredentialPath -Force
    [Array]::Clear($bytes,0,$bytes.Length)
    $plain=$null
    $password.Clear()
    $form.DialogResult=[Windows.Forms.DialogResult]::OK
    $form.Close()
})
[void]$form.ShowDialog()
$password.Clear()
$form.Dispose()
