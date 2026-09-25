# 低温联锁规程 · 离线补丁补传系统

两台维护终端离线编辑规程后补传，值班长基于服务端权威文本建立草案并提交
插入/删除补丁。服务端对迟到补丁做操作变换（OT），保证两份并发补丁无论
提交先后都收敛到同一文本。

## 运行

```bash
# 页面 + 接口 + 健康检查，宿主机端口可用 APP_PORT 配置（默认 8000）
docker compose up --build
# 自定义宿主机端口
APP_PORT=9000 docker compose up --build
```

打开 http://localhost:8000 ：

* **建立草案**：读取服务端当前全文与修订号，作为本终端基准；
* 提交一次 **插入**（`pos`,`text`）或 **删除**（`lo`,`hi`, 可选 `length`）
  补丁；页面立即显示服务端确认的全文、连续修订号与实际落点（规范化结果）；
* 本终端基准修订与最近确认结果保存在浏览器 localStorage，刷新后从真实
  接口恢复权威状态，本地旧草案不会被当作已批准内容。

## 验收

```bash
docker compose up --build --abort-on-container-exit --exit-code-from verify
```

`verify` 容器在业务检查之间穿插执行代码测试、构建检查与接口冒烟，
全部完成即退出，**退出码即验收结论**（0 通过，非 0 失败）：

1. 接口冒烟：健康检查、页面与静态资源；
2. **业务复核 A**：同位置并发插入按补丁标识字典序，两种提交序逐字符收敛；
3. 代码测试：OT 变换穷举/随机收敛、业务规则、原子持久化（32 项）；
4. 构建检查：`compileall` + `node --check`；
5. 接口冒烟：草案/文档一致、404、非法 JSON 400；
6. **业务复核 B**：删除区间内并发插入，两种提交序均保留插入内容；
7. 重启持久化：旧修订补丁历史转换、同载荷重传幂等不增修订；
8. **业务复核 C**：未来修订/越界/长度不符/标识复用被拒绝后，全文与
   修订号逐字节不变。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | `{status, revision}` |
| GET | `/api/document` | 当前权威全文与修订号 |
| POST | `/api/drafts` | 建立草案：当前全文 + 基准修订号 |
| POST | `/api/patches` | 提交补丁（见下） |

补丁请求：

```json
{"id": "term1-7", "kind": "insert", "base_revision": 3,
 "payload": {"pos": 12, "text": "紧急切断阀已复位\n"}}
```

```json
{"id": "term2-2", "kind": "delete", "base_revision": 3,
 "payload": {"lo": 40, "hi": 56, "length": 16}}
```

`length` 可省略；若同时给出且与 `hi-lo` 不一致则拒绝。

## 语义与规则

* **迟到补丁**：依次变换越过基准修订之后的已确认补丁（insert/insert、
  insert/delete、delete/insert、delete/delete 四种情形，见 `app/ot.py`）；
* **同位置并发插入**：按补丁标识 `id` 字典序决定先后，与到达顺序无关；
* **插入跨越删除**：插入点落在并发删除区间内时，内容保留在删除空隙处；
  从删除一侧看删除被插入切成两段，插入文本同样保留；
* **重叠删除**：删除字符集取并集，不重复删除；
* **幂等**：同一 `id` 携相同载荷重传，复现首次文本、修订与落点，
  不新增修订；
* **拒绝**（HTTP 409/400，规程文本与修订号不变）：`id` 复用但载荷不同、
  `base_revision` 为未来修订、位置/区间越界、删除长度不符、空插入；
* **原子确认**：每条补丁的规范化结果（pieces）与新全文、新修订号在同一次
  临时文件 + `os.replace` 原子替换中落盘；
* **重启**：历史补丁（含各自基准全文）持久化在 `/data/state.json`，
  重启后仍可转换旧修订上的合法补丁。

## 无容器本地开发

仅需 Python 3.11（标准库）；前端为原生 JS：

```bash
pip install pytest          # 可选
python scripts/run_tests.py # 或 pytest
DATA_FILE=./data/state.json PORT=8000 python -m app.server
APP_URL=http://127.0.0.1:8000 python scripts/verify.py
```
