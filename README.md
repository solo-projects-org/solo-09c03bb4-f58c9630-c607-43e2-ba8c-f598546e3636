# 景区客流报表补仓 API

旧日报缺页后，用留存的**跨日客流报表**补齐日明细的 FastAPI 服务。
SQLite 留存现行报表，提供整批补录、冲突整批退回、日人次唯一值查询。

## 业务规则

- 每份报表含：编号、首天、末日（均计入）、档期总人次；日人次为非负整数。
- 单份报表档期（末−首+1）**最长 90 天**；库内现行报表**最多 120 份**。
- 支持成批补录；已有编号按**整份替换**处理（档期、总量、版本整体更新）。
- 每批都与**入库时已生效**的报表一起核对：
  - 能同时成立才整体提交（单事务，失败回滚，不会出现半批入库）；
  - 无法同时成立则**整批退回**（HTTP 409），返回一组冲突报表编号，
    该组是**最小冲突组**——去掉组内任一份后，组内其余报表即可自洽。
- 查数只用同一套已生效报表：
  - 只有能从全部现行报表**唯一确定**的日人次才供数（`determined` + 整数值）；
  - 其余被覆盖但不唯一、或未被任何报表覆盖的日期一律标为**待补**（`pending`）；
  - 更正/替换报表后，失去唯一依据的日人次会自动转回待补（查询按现行报表实时求解）。

## 算法要点

把报表总和约束转成**差分约束系统**：

- 令前缀和 `P_d = 首天..d 的累计人次`，锚点 `P_0 = 0`；
- 报表 `[s,e] 总量 t` 等价于 `P_e - P_{s-1} = t`（拆成 `>=t` 与 `<=-t` 两条边）；
- 日人次非负等价于相邻前缀点之间 `P_d - P_{d-1} >= 0` 的权 0 链边。

图中只放报表端点（压缩图，节点 ≤ 241），用 numpy Floyd-Warshall 求全源最长路：

- 对角线出现正值 ⇔ 存在正权环 ⇔ 报表集合不可行；
- 对每个被覆盖日期，由最长路/最短路推出当日人次的最小值与最大值，
  两者相等且有限即为唯一确定；整数边权保证结果为整数。
- 冲突组用删除法求最小不可行子集（IIS）：逐个尝试删除，删除后仍不可行的成员不是必要成员。

## 目录结构

```
app/
  main.py       FastAPI 路由（/docs 即 Swagger）
  schemas.py    请求/响应模型
  service.py    业务服务：整批原子提交、容量护栏、冲突诊断、求解缓存
  solver.py     差分约束求解器（纯函数）
  database.py   SQLite / SQLAlchemy 持久化
tests/          pytest：单元、随机暴力校验、属性测试、API 集成、规模压测
Dockerfile / docker-compose.yml
```

## 用 Docker Compose 启动

```bash
docker compose up --build
# 服务：http://localhost:8000
# Swagger：http://localhost:8000/docs  （根路径 / 会自动跳转）
# 健康检查：http://localhost:8000/health
```

SQLite 数据文件落在命名卷 `scenic-data` 的 `/data/scenic.db`（WAL 模式），
容器重建后数据保留。

## Swagger 操作示例

1. `POST /reports/batch` 补录第一批：

   ```json
   {"reports": [
     {"report_id": "A", "start_date": "2024-03-01", "end_date": "2024-03-03", "total": 5},
     {"report_id": "B", "start_date": "2024-03-02", "end_date": "2024-03-02", "total": 2}
   ]}
   ```

2. `GET /daily?start_date=2024-03-01&end_date=2024-03-03` 查数：
   3 月 2 日唯一为 2，其余日期 `pending`。
3. 再补一份与现行报表矛盾的报表会得到 `409`，响应体 `detail.conflict_report_ids`
   即最小冲突组；同批其它报表随整批退回，不会入库。
4. 同编号再次提交即整份替换，响应里 `replaced_ids` 与自增 `version` 可查。

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/reports/batch` | 整批补录（替换/新增），冲突 409 整批退回 |
| GET  | `/reports` | 列出现行报表 |
| GET  | `/daily` | 区间查数，缺省区间覆盖全部报表范围 |
| GET  | `/health` | 健康检查 |

## 本地自测

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
SCENIC_DB_PATH=/tmp/scenic.db .venv/bin/pytest -q
SCENIC_DB_PATH=/tmp/scenic.db .venv/bin/uvicorn app.main:app --port 8000
```

测试包含：

- 求解器单元测试（可行性、唯一确定、零总量、嵌套/交错区间、同编号替换）；
- 60 组随机小实例的**暴力穷举校验**（枚举所有非负整数日向量核对可行性与逐日界）；
- 200 组随机实例的最小冲突组删除性质；
- API 集成测试（原子回滚、容量上限、替换后版本与待补回转、持久化视图）；
- 120 份 × 最长 90 天的规模压测（单次求解约 15ms，冲突诊断约 0.6s）。
