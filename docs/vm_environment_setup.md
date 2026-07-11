# Windows 交易环境从零搭建手册

> 适用场景:①新 Mac 搬家(拷贝虚拟机 或 从零重建);②Fusion/UTM 宿主对比实验;③灾难恢复。
> 2026-07-10 首版。核心原则:**代码与数据都在 Syncthing 同步网里,虚拟机只是可重建的壳**。

---

## 路线A:整机拷贝(最快,搬家首选)

1. 旧 Mac:关闭虚拟机(Windows 内正常关机);
2. 拷贝虚拟机包到新 Mac(外置盘/局域网):
   - UTM:`~/Library/Containers/com.utmapp.UTM/Data/Documents/*.utm`(或 UTM 界面里右键虚拟机 → 在访达中显示);
   - Fusion:`~/Virtual Machines.localized/*.vmwarevm`;
3. 新 Mac 装同款宿主软件,打开拷来的虚拟机包,启动;
4. Fusion 首次启动若询问 "Moved or Copied?" → 选 **I Copied It**;
5. 跳到【七、验证清单】。QMT/Python/Syncthing 全部原样带走,无需重装。

## 路线B:从零重建(以下各节)

---

## 一、创建 Windows 11 ARM 虚拟机

### Fusion 分支
1. 官网(support.broadcom.com,免费账号)下载 Fusion dmg,安装;
2. File → New → **Get Windows from Microsoft**(自动下载官方 Windows 11 ARM 镜像);
3. 配置:内存 6~8GB(16G 宿主)、磁盘 ≥80GB、CPU 4核;
4. 安装 **VMware Tools**(虚拟机菜单 → Install VMware Tools)。

### UTM 分支
1. `brew install --cask utm`;
2. 用 CrystalFetch(App Store 免费)下载 Windows 11 ARM ISO;
3. UTM 新建 → 虚拟化 → Windows,挂 ISO 安装;
4. 装 SPICE Guest Tools(网络/显示驱动);
5. **不要开启 UTM 目录共享**(WebDAV 慢;SMB 有签名 bug,见 docs/qmt_live_integration.md)。

### Windows 通用初始化
- 本地账户即可;时区 **UTC+8 北京**,勾选自动同步时间(交易系统对时间敏感!);
- 电源设置:永不睡眠、永不关屏;
- Windows Update 打完补丁(Prism x86 转译层随系统更新,对 QMT 稳定性重要)。

---

## 二、Python 3.11(⚠️ 必须 x64 版,不是 ARM64!)

xtquant 只发布 x86/x64 版,ARM64 Python 装不了。ARM Windows 会用 Prism 自动转译运行 x64 Python。

1. python.org → 下载 **Windows installer (64-bit)**(x64,不要选 ARM64)3.11.x;
2. 安装勾选 "Add python.exe to PATH";
3. 验证:`py -3.11 -c "import platform;print(platform.machine())"` → 应输出 `AMD64`;
4. 装依赖:`cd C:\A_System && py -3.11 -m pip install -r requirements.txt`。

---

## 三、QMT / miniQMT

1. 国金证券官网/营业部渠道下载 QMT 交易端安装包(x86 程序,Prism 转译运行);
2. 安装到默认路径,记下 `userdata_mini` 目录位置(如 `C:\Users\<用户>\国金证券QMT交易端\userdata_mini`);
3. 设置环境变量 `QMT_PATH` 指向该目录(系统属性 → 环境变量);
4. 登录:输账号密码即可(国金账户无机器绑定,换机器直接登;万一提示环境异常再联系客户经理);
5. **先登模拟盘验证**,确认行情、委托正常。

---

## 四、Syncthing(数据自动到位的关键)

1. `winget install Syncthing.Syncthing`(自动选 ARM64 原生版);
2. 重开 PowerShell,运行 `syncthing`,浏览器开 `127.0.0.1:8384`;
3. 操作 → 显示 ID,把设备 ID 告知 Mac 端(Claude 可在 Mac 端命令行完成配对与共享);
4. 接受 Mac 的设备请求和文件夹共享,**路径填 `C:\A_System`**;
5. 文件夹编辑 → 忽略模式,粘贴(两端各配一份,不会自动同步):
   ```
   daemon_pid
   *.pid
   (?d).sync-conflict-*
   (?d).DS_Store
   (?d)Thumbs.db
   __pycache__
   *.pyc
   .stfolder
   .stversions
   ```
6. 开机自启(普通 PowerShell):
   ```powershell
   $st=(Get-Command syncthing).Source
   $ws=New-Object -ComObject WScript.Shell
   $sc=$ws.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Syncthing.lnk")
   $sc.TargetPath=$st
   $sc.Arguments="serve --no-console --no-browser"
   $sc.Save()
   ```
7. 等首次同步收敛(两边状态都显示"最新")。

Mac 端(新 Mac 时):`brew install syncthing && brew services start syncthing`,配对同上;关外部中继:
```
syncthing cli config options relays-enabled set false
syncthing cli config options global-ann-enabled set false
```

---

## 五、启动交易系统

```powershell
cd C:\A_System
py -3.11 start_windows.py
```
- 防呆闸:从 Z:/UNC 路径启动会被拒绝,必须在 C:\A_System 下;
- 启动门禁应在几秒内完成(本地盘基准 2~3 秒)。

---

## 六、安全纪律(多机并存期必读)

1. **同一 QMT 实盘账户绝不在两台虚拟机同时登录**(互踢);新环境测试用模拟盘,或先退出旧环境;
2. **实盘 daemon 全网只允许一个实例**——新环境验证期间只跑纸面模式,切换 = 旧机停 daemon → 新机启 daemon,永不双开;
3. 对比实验期,测试机的 Syncthing 文件夹建议设"仅接收"(receive-only),防测试写入污染生产数据;
4. 16G 宿主不要双开虚拟机,错峰运行。

---

## 七、验证清单(每次搬家/重建后,先过全绿再上实盘)

- [ ] Windows 时间 = 北京时间,自动同步开启
- [ ] `py -3.11 -c "import platform;print(platform.machine())"` → AMD64
- [ ] `py -3.11 -c "import xtquant"` 无报错
- [ ] QMT 模拟盘登录成功,行情正常
- [ ] Syncthing 两端状态"最新",改一个文件 10 秒内出现在对面
- [ ] `cd C:\A_System && py -3.11 start_windows.py` 门禁秒级通过、账户验证成功
- [ ] 日志确认:晨检哨兵/决策链正常输出
- [ ] (实盘切换日)旧机 daemon 已停,QMT 已退出
