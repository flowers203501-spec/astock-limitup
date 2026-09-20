# A股涨停复盘网站

每个交易日收盘后自动生成:涨停 / 炸板 / 跌停家数、**封板率**、**炸板率**、**连板天梯**、昨日涨停晋级率、涨停行业分布,以及近 30 日走势。

纯静态网站:`fetch_data.py` 每天生成 JSON,`index.html` 读取并渲染,没有后端、没有前端依赖。

```
index.html          页面(单文件,无第三方库)
fetch_data.py       数据脚本:AkShare 抓取 -> 计算指标 -> data/*.json
data/               每日数据 + index.json(趋势用)
.github/workflows/  每个工作日 15:40 自动更新并提交
```

## 本地运行

```bash
pip install -r requirements.txt
python fetch_data.py --backfill 20     # 回补最近 20 个交易日(东方财富通常只保留最近约一个月)
python -m http.server 8000             # 然后打开 http://localhost:8000
```

不要直接双击打开 index.html,浏览器会拦截本地 JSON 读取。

之后每天收盘后(15:30 以后)运行一次 `python fetch_data.py` 即可。

## 部署(免费,GitHub Pages + Actions)

1. 把项目推到 GitHub 仓库。
2. Settings → Pages → Source 选 `Deploy from a branch`,分支选 `main`、目录 `/ (root)`。
3. Settings → Actions → General → Workflow permissions 选 `Read and write permissions`。
4. Actions 页手动运行一次 `update-data`(可填回补天数),之后每个工作日 15:40(北京时间)自动更新。

GitHub 的服务器在海外,偶尔会访问不到东方财富接口。如果 Action 经常失败,改在自己的电脑或国内云服务器上用 cron 跑:

```cron
40 15 * * 1-5  cd /path/to/astock-limitup && python fetch_data.py && git add data && git commit -m data && git push
```

## 指标口径

| 指标 | 定义 |
|---|---|
| 涨停 | 收盘涨停的股票数(东方财富涨停股池) |
| 炸板 | 盘中触及涨停、收盘没封住的股票数(炸板股池) |
| 封板率 | 涨停 ÷ (涨停 + 炸板) |
| 炸板率 | 炸板 ÷ (涨停 + 炸板),等于 1 − 封板率 |
| 连板天梯 | 按东方财富的「连板数」分组 |
| 晋级率 | 昨日涨停的股票中今日继续涨停的比例,按昨日连板数分层 |
| 一字 / 回封 | 09:25 集合竞价封死且未打开 / 盘中打开过又重新封上 |

不同软件的封板率口径略有差异(有的把「曾涨停」作分母、有的剔除新股)。想改口径只需要改 `fetch_data.py` 里 `build_day` 的 `summary` 部分。默认剔除 ST,想保留把 `EXCLUDE_ST` 改成 `False`。

## 注意

- 数据来自东方财富公开接口(经 AkShare),接口字段或限流策略可能变化;脚本对涨停池和炸板池抓取失败会直接放弃当天,避免写入错误的封板率。
- 仅供复盘学习,不构成投资建议;对外公开运营前请留意数据源的使用条款。
