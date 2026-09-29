# 开源发布指南（GitHub）

本指南面向「第一次在 GitHub 发布项目」的人，从打开网页到仓库上线，一步步来。
目标：把 NAS Safe 的核心引擎 + 时间轴 UI 以 **MIT 协议**免费开源，建立用户信任、作为获客入口。

---

## 一、协议说明（为什么用 MIT）

- **MIT 是最宽松、最被广泛采用的协议**：别人可以免费使用、修改、再分发，甚至闭源商用，只要保留你的版权声明。
- 对你的价值：**降低用户信任门槛**。用户看到 MIT + 公开源码，才敢把"最高权限"交给你。
- 本项目开源范围：**核心快照引擎（storage.py / app.py）+ 时间轴 UI（web/）**。
- 进阶功能（自动策略、告警、AI、异地副本）后续将以**闭源 License**形式提供，不在本开源仓库内。
- 协议文件：`LICENSE`（已存在于仓库根目录）。

> 注：若未来集成 `httm`（MPL 2.0 协议）的源码而非仅调用其命令行，需注意协议兼容——
> MPL 是"文件级"开源协议，含 httm 源码的文件需保持 MPL。当前 MVP 未集成，全仓库用 MIT 即可。

---

## 二、在 github.com 上建仓库（网页操作）

1. 打开 https://github.com/ 并登录你的账号。
2. 右上角点 **"+"**（加号）→ 选 **"New repository"**（新建仓库）。
3. 填写：
   - **Repository name（仓库名）**：`nas-safe`
   - **Description（描述，可选）**：`给你的 NAS 上一把锁和一个傻瓜时间轴。跨品牌 btrfs/ZFS 快照管理 + 防勒索。`
   - **Visibility（可见性）**：选 **Public**（公开 = 免费开源；Private 是私有要付费团队）
   - **不要**勾选 "Add a README file"（我们已经有 README，避免冲突）
   - **不要**勾选 "Add .gitignore"（我们已有）
   - **不要**勾选 "Choose a license"（我们已有 LICENSE 文件）
4. 点 **"Create repository"**（创建仓库）。

创建后，GitHub 会显示一个空仓库页面，里面有一串命令（如 `git remote add origin https://github.com/你的用户名/nas-safe.git`）。**先别急着照抄那串命令**，按下面的本地步骤走更稳。

---

## 三、本地推送到 GitHub（在 E 盘项目目录执行）

> 以下命令在 **Git Bash** 里运行（不是 CMD / PowerShell）。
> 路径换成你实际的目录：`E:/我的AI软件/NAS快照AI工具`

```bash
# 1. 进入项目目录
cd "/e/我的AI软件/NAS快照AI工具"

# 2. 配置本机 git 身份（只需一次，换成你自己的名字和邮箱）
git config --global user.name "你的名字"
git config --global user.email "你的邮箱@example.com"

# 3. 初始化仓库并提交
git init
git add .
git commit -m "NAS Safe 首版：跨品牌 btrfs/ZFS 快照时间轴 + 防勒索 MVP"

# 4. 关联远程仓库（把 你的用户名 换成你的 GitHub 用户名）
git remote add origin https://github.com/你的用户名/nas-safe.git

# 5. 推送到 GitHub
git branch -M main
git push -u origin main
```

**push 时会要求登录 GitHub**。弹窗里选：
- 有 GitHub 账号密码登录的，直接登录；
- 现在 GitHub 一般要求用 **Personal Access Token（个人访问令牌）** 当密码。
  生成地址：GitHub → 右上角头像 → Settings → Developer settings → Personal access tokens → Tokens (classic) → Generate new token，勾选 `repo` 权限，复制下来当密码用（只显示一次，存好）。

---

## 四、发布前检查清单

- [x] `LICENSE` 文件存在（MIT）← 已补
- [x] `README.md` 有吸引人的开头和说明
- [x] `.gitignore` 已排除缓存/快照目录
- [ ] **确认没有把任何密钥、密码、Token 提交进去**（本项目目前不涉及，但养成习惯）
- [ ] 在 GitHub 仓库的 **Settings → General → Social preview** 上传一张封面图（用 `docs/ui-main.png` 即可），分享时更好看
- [ ] 在仓库 **About** 栏填写简介，并勾选 "Releases" 显示最新版本

---

## 五、发布后怎么传播（这一步比建仓库重要）

开源不是为了"放在那就有人来"，而是为了**在社区建立信任 + 引流**：

1. **飞牛社区 / 绿联论坛**：发帖标题别写"我做了个工具"，写
   《飞牛用户注意：你的快照可能救不了你——我做了个工具补上这个洞》。
   正文讲两件事：①系统自带快照管理员能删（绿联官方文档原话）；②开源工具怎么补。
2. **什么值得买 / B 站**：录一段 30 秒动图——时间轴往左一拖，文件复原。
3. **GitHub 本身**：在 README 放一张界面截图 + 一行安装命令
   （`docker run -d --privileged -v /:/host:ro -p 8848:8848 tsetch/nas-safe`），
   小白复制粘贴就能跑。
4. **关键**：开源核心、闭源进阶。免费版解决"我今天误删文件"，付费版解决"我中了勒索"。
   用户靠免费版救回一次数据，会主动帮你传播——这是零成本的广告。

---

## 六、常见问题

**Q：公开仓库免费吗？**
A：完全免费。GitHub 的 Public 仓库对个人永久免费，不限数量。

**Q：开源了别人拿去卖怎么办？**
A：MIT 允许别人再分发甚至闭源商用，只要你保留版权声明。但你的商业模式是
"开源核心 + 闭源进阶服务"，卖点本就不在代码本身，而在持续服务和信任。
别人能跑你的核心，反而证明你东西好——付费的是"省事 + 告警 + 异地副本"。

**Q：以后进阶功能闭源，和 MIT 冲突吗？**
A：不冲突。MIT 只约束**当前仓库里的代码**。你后续单独发布闭源二进制/进阶模块，
是独立产品，不受本仓库 MIT 约束。只需在 README 写清"哪些是开源、哪些是闭源"即可。

**Q：能不能先私有、以后再公开？**
A：可以。Private 仓库随时能改成 Public（Settings → Change visibility）。
但建议一开始就 Public——信任是这项目的核心资产，早公开早积累。
