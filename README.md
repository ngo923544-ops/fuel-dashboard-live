# 航空燃油价格预测看板（自动更新版）

看板页面与数据分离：`index.html` 负责展示与预测，`data.json` 是唯一的数据文件，由
`scripts/update_data.py` 通过 GitHub Actions **每个工作日自动抓取更新**，无需人工维护。

## 工作原理

```
GitHub Actions (工作日定时)
   │
   ├─ 抓取商务部 price.mofcom.gov.cn  →  MOPS 航空煤油（日频，美元/桶）
   ├─ 抓取 EIA API v2                 →  US Gulf 航油 + Brent 原油（日频）
   │
   └─ 合并写入 data.json 并提交
            │
            ▼
Netlify / GitHub Pages 自动部署
            │
            ▼
index.html 打开时 fetch('data.json')
   ├─ 成功 → 与内置基线数据合并（新数据覆盖同日期旧值）→ 显示绿色"自动更新已启用"徽章
   └─ 失败 → 回退到内置基线数据（日频，截至 2026-08-11）→ 显示黄色"内置数据模式"徽章
```

双保险设计：即使抓取连续失败，页面仍能用内置数据正常打开，只是徽章会提示数据未更新。

## 部署步骤（约 10 分钟，一次性）

1. **创建 GitHub 仓库**：把本文件夹的全部内容推送上去（保持目录结构）。

   ```bash
   cd fuel-dashboard-live
   git init
   git add .
   git commit -m "fuel dashboard with auto-updating data"
   git branch -M main
   git remote add origin https://github.com/<你的账号>/fuel-dashboard-live.git
   git push -u origin main
   ```

2. **（可选但推荐）配置 EIA API Key**：
   到 https://www.eia.gov/opendata/ 免费注册一个 key，
   在仓库 Settings → Secrets and variables → Actions 里新建 secret，
   名称 `EIA_API_KEY`，值填你的 key。不配置也能用公共 DEMO_KEY，只是限流更严。

3. **连接部署平台**（二选一）：

   - **Netlify**（推荐，你已经在用）：New project from Git → 选这个仓库，
     构建设置留空（纯静态站，Publish directory 填 `.`）。之后每次 Actions 提交
     data.json，Netlify 会自动重新部署。
   - **GitHub Pages**：仓库 Settings → Pages → Source 选 Deploy from a branch → main / root。

4. **验证**：打开站点，标题栏应出现绿色徽章"自动更新已启用 · MOPS … · US Gulf … · Brent …"。

## 定时任务说明

- 默认 cron：`30 9 * * 1-5`（UTC 时间工作日 09:30，即北京时间 17:30），
  此时商务部当日价格通常已发布。可在 `.github/workflows/update-data.yml` 中调整。
- 也可在仓库 Actions 页面点 **Run workflow** 手动触发一次。
- GitHub 定时任务可能有几分钟到半小时的延迟，属正常现象。
- 抓取失败时：单源失败只更新另一源并标记 `partial`；两源全失败则不改动
  data.json 并让 Actions 运行显示为红色（可在仓库 Settings → Notifications
  里配置失败邮件通知）。

## 数据源与更新频率的现实上限

| 序列 | 来源 | 频率 | 发布规律 |
| --- | --- | --- | --- |
| MOPS 航空煤油（新加坡 FOB） | 商务部价格监测中心 | 日 | 每个工作日下午更新前一交易日 |
| US Gulf 航煤现货 | EIA | 日 | 滞后约一周发布 |
| Brent 原油现货 | EIA | 日 | 滞后约一周发布 |

所以看板的"实时"上限是 **T+1 日更**（MOPS），EIA 两条日线滞后约一周自动补齐——
相比月线（滞后 4–6 周）已经能第一时间捕捉到当月行情波动。

## 本地预览

`data.json` 通过 HTTP 加载，直接双击 index.html（file:// 协议）会触发回退模式，
这是预期行为。本地完整预览可以用任意静态服务器，例如装了 Node 的话：
`npx serve .`，或 VS Code 的 Live Server 插件。

## 文件结构

```
fuel-dashboard-live/
├── index.html                      # 看板页面（含内置基线数据 + 自动合并逻辑）
├── data.json                       # 数据文件（由脚本自动维护，勿手改）
├── scripts/update_data.py          # 抓取脚本（纯 Python 标准库，无依赖）
├── .github/workflows/update-data.yml  # 定时任务
└── README.md
```
