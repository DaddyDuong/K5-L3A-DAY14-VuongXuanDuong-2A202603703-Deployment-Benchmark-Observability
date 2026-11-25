# Benchmark & Observability Report — Day 14

> Mọi con số trong báo cáo này được thu thập trực tiếp từ các file JSON trong `reports/`, file CSV trong `benchmark-results/`, Prometheus/Jaeger API và ảnh chụp màn hình trong `screenshots/` — không ước lượng.

**Họ tên:** Vuong Xuan Duong (MSSV: 2A202603703)
**Ngày chạy:** 2026-11-25
**Môi trường:** Arch Linux, AMD Ryzen 7 7840HS (16 vCPUs), 27 GiB RAM, Python 3.14.10 (local venv cho `develop/`), Docker + Docker Compose (`python:3.11-slim` cho `production/`)

---

## 1. SLO của agent

| SLI | SLO | Lý do chọn ngưỡng này |
|---|---|---|
| p95 latency `/ask` | `< 500 ms` | Mock LLM có base latency ~120–180 ms + retrieval ~20 ms (~190–220 ms bình thường) và ~3% tail latency (+1500 ms). Ngưỡng p95 `< 500 ms` (khớp mặc định của `load_test.py`) đảm bảo 95% người dùng nhận câu trả lời dưới nửa giây, đồng thời phát hiện ngay khi hệ thống bão hoà hàng đợi hoặc tỉ lệ slow request vượt quá 5%. Alert Prometheus đặt lỏng hơn (`p95 > 1000 ms` trong `for: 2m`) để tránh alert fatigue do spike ngắn. |
| Error rate `/ask` | `< 2.0%` | Mock upstream LLM có tỉ lệ lỗi 503 tự nhiên ~1% (`MOCK_ERROR_RATE=0.01`). Đặt SLO `< 2.0%` (error budget = 2%) tạo biên an toàn cho dao động ngẫu nhiên nhỏ nhưng vẫn chặn suy giảm nghiêm trọng. Alert Prometheus đặt ở `> 5%` trong `for: 1m` để chỉ báo động khi lỗi thật sự kéo dài. |
| Accuracy trên golden dataset | `≥ 80%` (CI gate production: `≥ 85%`, sau sửa KB: `100%`) | Bộ `golden_dataset.jsonl` gồm 23 câu hỏi kiến thức cốt lõi về Deployment/Observability. Ngưỡng tối thiểu 80% (85% trong CI) + yêu cầu **0 regression** so với baseline đảm bảo phiên bản mới không làm giảm chất lượng so với bản đang chạy production. |

---

## 2. Benchmark hiệu năng (`develop/`, `load_test.py`)

### 2.1 Concurrency vs latency (agent v2, 300 requests, `MOCK_SLOW_RATE=0.03`)

Nguồn dữ liệu: `reports/load_v2_c1.json`, `reports/load_v2_c10.json`, `reports/load_v2_c50.json`, `reports/load_v2_c100.json`.

| Concurrency | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Error rate | SLO |
|---:|---:|---:|---:|---:|---:|---|
| 1 | 3.96 | 190.0 | 220.8 | 1709.2 | 0.33% (1/300) | ✅ PASS |
| 10 | 41.77 | 188.0 | 219.6 | 1675.6 | 1.00% (3/300) | ✅ PASS |
| 50 | 107.43 | 189.5 | 241.5 | 1698.0 | 0.33% (1/300) | ✅ PASS |
| 100 | 68.74 | 305.2 | 2498.2 | 3031.3 | 0.33% (1/300) | ❌ FAIL |

Chi tiết bổ sung từ các file JSON:
- `c=1`: `duration_s=75.84`, `avg=253.3 ms`, `max=1715.7 ms`, `status_codes={'200': 299, '503': 1}`, `total_cost_usd=$0.010152`.
- `c=10`: `duration_s=7.18`, `avg=222.9 ms`, `max=1711.0 ms`, `status_codes={'200': 297, '503': 3}`, `total_cost_usd=$0.010021`.
- `c=50`: `duration_s=2.79`, `avg=250.2 ms`, `max=1709.0 ms`, `status_codes={'200': 299, '503': 1}`, `total_cost_usd=$0.010142`.
- `c=100`: `duration_s=4.36`, `avg=834.6 ms`, `max=4123.7 ms`, `status_codes={'200': 299, '503': 1}`, `total_cost_usd=$0.010090` — vi phạm SLO p95 (`2498.2 ms > 500 ms`).

**Nhận xét:** RPS **không tăng tuyến tính** theo concurrency:
1. Từ `c=1 → c=10`: RPS tăng ~10.5x (`3.96 → 41.77 req/s`) gần như tuyến tính vì agent chạy bất đồng bộ (`async/await` trên Uvicorn) và phần lớn thời gian là I/O wait (`asyncio.sleep` mô phỏng gọi LLM), nên 1 process xử lý song song 10 request mà hầu như không tăng p50/p95 (`190.0 → 188.0 ms`).
2. Từ `c=10 → c=50`: RPS chỉ tăng ~2.57x (`41.77 → 107.43 req/s`), dù concurrency tăng 5x. Lý do: ở cuối batch 300 request, thời gian hoàn thành bị chi phối bởi các tail-latency request (~1.7 s) còn đang chạy (straggler effect) và bắt đầu xuất hiện chi phí scheduling/JSON serialization trên 1 event loop (p95 nhích từ `219.6 → 241.5 ms`).
3. Tại `c=100`: hệ thống **bão hoà (saturation)** — RPS thậm chí **giảm** xuống `68.74 req/s`, p50 tăng lên `305.2 ms`, còn p95 vọt lên **`2498.2 ms`** (gấp >10 lần, FAIL SLO) và max lên `4123.7 ms`. Khi quá nhiều request đổ vào 1 worker duy nhất cùng lúc, event loop + socket accept queue + JSON logging/Prometheus histogram updates bị tranh chấp CPU, gây hiệu ứng xếp hàng (queueing delay) làm latency tăng theo hàm mũ theo định luật Little / lý thuyết hàng đợi.

### 2.2 Tail latency (`MOCK_SLOW_RATE`, agent v2, 300 requests, concurrency=20)

Nguồn dữ liệu: `reports/load_v2_slow_0.json`, `reports/load_v2_slow_0.03.json`, `reports/load_v2_slow_0.1.json`.

| `MOCK_SLOW_RATE` | avg (ms) | p95 (ms) | p99 (ms) | RPS | Error rate | SLO |
|---|---:|---:|---:|---:|---:|---|
| 0 | 187.6 | 216.7 | 220.7 | 103.87 | 1.33% (4/300) | ✅ PASS |
| 0.03 (mặc định) | 214.5 | 219.5 | 1682.6 | 74.79 | 1.00% (3/300) | ✅ PASS |
| 0.1 | 385.6 | 1699.9 | 1716.7 | 44.74 | 1.33% (4/300) | ❌ FAIL |

**Nhận xét:**
- **p99 và p95 nhạy nhất** với các request chậm bất thường:
  - Khi tăng `MOCK_SLOW_RATE` từ `0 → 0.03`: `avg` chỉ tăng nhẹ `+26.9 ms` (`187.6 → 214.5 ms`) và `p95` gần như đứng yên (`216.7 → 219.5 ms`), nhưng **`p99` nhảy vọt gấp 7.6 lần** từ `220.7 ms → 1682.6 ms`!
  - Khi tăng lên `0.1` (10% request chậm, vượt ngưỡng 5%): **`p95` cũng vọt gấp 7.8 lần** từ `219.5 ms → 1699.9 ms` (FAIL SLO `≤ 500 ms`), trong khi `avg` chỉ là `385.6 ms` (vẫn dưới 500 ms nếu đặt SLO theo average!).
- **Vì sao SLO không nên đặt theo average:** giá trị trung bình bị 90–97% request nhanh "kéo xuống" và che giấu hoàn toàn nhóm người dùng đang phải chờ gần 2 giây. Nếu đặt SLO `avg < 500 ms`, cả mức `MOCK_SLOW_RATE=0.1` (`avg = 385.6 ms`) vẫn trông như PASS dù cứ 10 người dùng thì 1 người phải chờ ~1.7 giây. Đặt SLO theo **p95 / p99** bảo vệ trực tiếp trải nghiệm tail-latency của người dùng.

---

## 3. Benchmark chất lượng (`develop/`, `eval/run_eval.py`)

Nguồn dữ liệu: `reports/eval_v1.json`, `reports/eval_v2.json`, `reports/eval_v2_fixed.json` (23 câu hỏi trong `eval/golden_dataset.jsonl`, giữ nguyên không chỉnh sửa).

| Chỉ số | Agent v1 | Agent v2 | Agent v2 (sau khi sửa KB) |
|---|---:|---:|---:|
| Accuracy | 4.35% (1/23) | 91.30% (21/23) | **100.0% (23/23)** |
| Latency p50 / p95 (ms) | 168.1 / 189.7 | 191.5 / 216.4 | 192.0 / 222.8 |
| Avg tokens / câu | 57.7 | 108.7 | 113.8 |
| Tổng cost (USD) | $0.000413 | $0.000756 | $0.000792 |
| Cost / câu đúng (USD) | $0.00041295 | $0.00003599 | **$0.00003443** |
| Quality gate | ❌ FAIL (`< 80%`) | ✅ PASS (0 regression) | ✅ PASS (`min=100%`, 0 regression) |

**Những câu v2 trả lời sai ban đầu và cách sửa** (chỉnh duy nhất `KNOWLEDGE_BASE` trong `develop/utils/mock_llm.py` và đồng bộ sang `production/utils/mock_llm.py`, **không sửa `golden_dataset.jsonl`**):

| ID | Keyword bị thiếu | Đã sửa gì |
|---|---|---|
| `security-02` | `token`, `hết hạn`, `chữ ký` | Thêm entry `(["jwt", "api key"], ...)` vào `KNOWLEDGE_BASE` giải thích sự khác biệt giữa API key cố định và JWT (token có chữ ký số, chứa thông tin user và thời điểm hết hạn). |
| `scaling-03` | `cpu`, `memory`, `custom metrics` | Thêm entry `(["hpa", "kubernetes", "autoscal"], ...)` vào `KNOWLEDGE_BASE` mô tả Kubernetes HPA tự động scale pod dựa trên CPU, memory hoặc custom metrics (RPS, độ dài hàng đợi). |

Kiểm chứng sau khi sửa và khởi động lại agent v2:
```bash
python eval/run_eval.py --url http://localhost:8002 --min-accuracy 1.0 \
  --baseline reports/eval_v2.json --out reports/eval_v2_fixed.json
# → Accuracy: 100.0% (23/23), Câu được cải thiện: 2, Câu bị regression: 0
# → Quality gate (accuracy ≥ 100%, không regression): ✅ PASS (exit code 0)
```

**Nhận xét về trade-off chất lượng / chi phí / latency giữa v1 và v2:**
- **Chất lượng:** v2 (có RAG retrieval) tăng accuracy từ **4.35% (1/23)** lên **91.30% (21/23)** và **100.0% (23/23)** sau khi bổ sung 2 tài liệu thiếu — cải thiện `+95.65 điểm %`.
- **Chi phí:** Mỗi câu hỏi v2 tốn gần gấp đôi số token (`113.8` vs `57.7` tokens/câu, `+97%`) và tổng cost (`$0.000792` vs `$0.000413`, `1.92x`) vì phải chèn tài liệu context vào prompt. Tuy nhiên, nếu tính theo **cost / câu trả lời đúng** (`cost_per_correct_usd`), v2 sau sửa KB chỉ tốn **`$0.00003443`** so với **`$0.00041295`** của v1 — **rẻ hơn ~12.0 lần** cho mỗi câu trả lời hữu ích!
- **Latency:** v2 chậm hơn v1 khoảng `~24 ms` ở p50 (`192.0 ms` vs `168.1 ms`) do thêm bước `search_knowledge_base` (~20 ms) và prompt dài hơn một chút. Đây là đánh đổi hoàn toàn xứng đáng trong thực tế sản xuất.

### 3.1 Exercise 7.3 — Quality Gate trong CI/CD

Kiểm thử thực tế hành vi chặn regression khi đánh giá v1 so với baseline v2:
```bash
python eval/run_eval.py --url http://localhost:8003 --baseline reports/eval_v2.json
# Accuracy: 4.3% (1/23) vs Baseline 91.3% (-87.0 điểm %)
# Câu bị regression: 20 ['docker-01', 'docker-02', ..., 'cloud-01']
# Quality gate (accuracy ≥ 80%, không regression): ❌ FAIL
# Exit code: 1
```

Vì `run_eval.py` thoát với **`exit code 1`** nếu `accuracy < --min-accuracy` **hoặc** có bất kỳ câu nào bị regression (baseline đúng → bản mới sai), ta tích hợp trực tiếp vào GitHub Actions để tự động fail PR và chặn merge:

```yaml
# .github/workflows/eval-quality-gate.yml
name: Agent Eval Quality Gate
on: [pull_request]

jobs:
  eval:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Start agent v2 and run golden-dataset eval
        working-directory: 06-lab-complete/benchmark-observability/develop
        run: |
          pip install -r requirements.txt
          AGENT_VERSION=v2 MOCK_ERROR_RATE=0 PORT=8002 python app.py &
          SERVER_PID=$!
          trap "kill $SERVER_PID 2>/dev/null || true" EXIT
          for i in $(seq 1 30); do
            curl -sf http://localhost:8002/health && break
            sleep 0.5
          done
          python eval/run_eval.py \
            --url http://localhost:8002 \
            --min-accuracy 0.85 \
            --baseline reports/eval_v2.json
```
Khi PR làm tụt accuracy dưới `85%` hoặc làm hỏng bất kỳ câu hỏi nào đã pass ở `reports/eval_v2.json`, lệnh trả về `exit code 1` → job `eval` bị đánh dấu failed → branch protection rule chặn nút Merge.

---

## 4. Observability stack (`production/`)

### 4.1 Screenshot & Unit test alert rules

**Kiểm tra unit test alert rules (`promtool`):**
```bash
cd 06-lab-complete/benchmark-observability/production
docker run --rm -v "$PWD/prometheus:/p" --entrypoint promtool \
  prom/prometheus:v3.16.0 test rules /p/alert_rules_test.yml
# Output:
# Processing test file: /p/alert_rules_test.yml
#   SUCCESS
```

**4 ảnh chụp màn hình đã kiểm chứng trong `screenshots/`:**
- [x] Dashboard Grafana khi đang có tải (30 users, 2 instances, thấy rõ 4 Golden Signals + Latency/Traffic + LLM + bảng phiên bản): [grafana-dashboard.png](screenshots/grafana-dashboard.png)
- [x] Một trace chậm trong Jaeger (`trace_id: 88ad808d379b0a5343819edc71fc2497`, mở rộng span `chat mock-llm-small` `1.7s` + input/output tokens): [jaeger-slow-trace.png](screenshots/jaeger-slow-trace.png)
- [x] Một trace lỗi trong Jaeger (`trace_id: 45fd55e26e99b694cbe9b20e3a60abdb`, HTTP 503, mở rộng tab `Details` → `Events (1)` thấy rõ event `exception` của `utils.mock_llm.LLMError`): [jaeger-error-trace.png](screenshots/jaeger-error-trace.png)
- [x] Trang Alerts của Prometheus với **3 alert cùng đang FIRING** (`AgentHighErrorRate`, `AgentHighLatencyP95`, `AgentInstanceDown`): [prometheus-alert-firing.png](screenshots/prometheus-alert-firing.png)

### 4.2 Trả lời câu hỏi Exercise 7.4 (30 users, 2 instances)

Số liệu thực tế đo từ Prometheus & Grafana lúc hệ thống chạy ổn định 30 users / 2 instances (`grafana-dashboard.png`):
1. **p95 và avg đang là bao nhiêu? Khoảng cách giữa chúng nói lên điều gì?**
   - Thực đo tại Prometheus: **`avg = 218.7 ms`**, **`p50 = 173.4 ms`**, **`p95 = 290.1 ms`** (Grafana hiển thị `291.95 ms`), **`p99 = 1756.0 ms`**.
   - Khoảng cách `p95 (290 ms) > avg (219 ms) > p50 (173 ms)` và đặc biệt bước nhảy từ `p95 (290 ms)` lên `p99 (~1756 ms)` cho thấy phân bố latency bị **lệch phải (right-skewed)** do ~3% request bị tail latency (+1500 ms). Biểu đồ *Latency /ask* trên Grafana cho thấy rõ đường `p99` nằm sát `1.8 s` (vùng đỏ vượt 1 s) trong khi `avg` và `p50` nằm thấp gần `200 ms`.
2. **Panel "Requests/s theo instance": Nginx có chia đều traffic cho 2 instance không?**
   - **Có.** Traffic tổng `/ask` là `17.75 req/s`, chia đều cho 2 instance: `172.27.0.4:8000` nhận **`9.07 req/s` (51.1%)** và `172.27.0.8:8000` nhận **`8.67 req/s` (48.9%)** nhờ thuật toán round-robin của Nginx (`upstream agent_cluster { server agent:8000 resolve; }`). Trên biểu đồ *Requests/s theo instance* cũng thấy rõ quá trình auto-discovery từ 1 instance → 3 instances (mỗi instance ~20 req/s khi chạy `scale_3`) → 2 instances (~9 req/s mỗi instance) mà không cần restart Nginx.
3. **Vì sao `input tokens/s` lớn hơn `output tokens/s`?**
   - Thực đo: **`input = 1358.2 tokens/s`** vs **`output = 708.8 tokens/s`** (input gấp **1.92 lần** output).
   - Nguyên nhân: agent v2 dùng RAG nên mỗi prompt gửi vào LLM bao gồm `SYSTEM_PROMPT` + đoạn tài liệu `context` lấy từ `KNOWLEDGE_BASE` + `question` của người dùng, trong khi phần output chỉ là câu trả lời ngắn gọn. Đây là đặc trưng rất phổ biến của các ứng dụng RAG/Agent trong thực tế.
4. **`curl -i http://localhost:8080/metrics` trả về gì? Vì sao?**
   - Trả về **`HTTP/1.1 404 Not Found`** từ `nginx/1.28.3`:
     ```http
     HTTP/1.1 404 Not Found
     Server: nginx/1.28.3
     ```
   - Trong `production/nginx/nginx.conf`, rule `location = /metrics { return 404; }` cố tình chặn mọi truy cập từ bên ngoài vào `/metrics`. Endpoint `/metrics` tiết lộ thông tin nội bộ nhạy cảm (danh sách route, phiên bản agent/model, lưu lượng truy cập, chi phí, tỉ lệ lỗi) — chỉ Prometheus chạy trong mạng Docker nội bộ `obs_net` mới được scrape trực tiếp `agent:8000/metrics`.

### 4.3 Thí nghiệm "phá hệ thống" (Chaos Experiments)

Giữ tải Locust liên tục `30 users / ramp 5` trên 2 instance, đo thời gian thực từ lúc áp dụng thay đổi tới lúc alert chuyển sang `firing` qua Prometheus API (`/api/v1/rules`):

| # | Thay đổi | Alert nào firing | Thời gian từ lúc đổi → firing | Panel nào phản ánh rõ nhất |
|---|---|---|---|---|
| A | `MOCK_ERROR_RATE=0.2` | `AgentHighErrorRate` (`for: 1m`, ngưỡng `> 5%`) | **`95.1s`** (`pending` lúc `+35.0s`, `firing` lúc `+95.1s`; error rate đạt **21.39%**) | Golden Signals *Error rate* (chuyển đỏ), *Requests/s theo status code* (`503` tăng mạnh), *LLM calls theo kết quả* (`error`) |
| B | `MOCK_SLOW_RATE=0.3` | `AgentHighLatencyP95` (`for: 2m`, ngưỡng `> 1s`) | **`145.1s`** (`pending` lúc `+25.0s`, `firing` lúc `+145.1s`; p95 đạt **1891.5 ms**) | Golden Signals *Latency p95* (chuyển đỏ), *Latency /ask — p50 / p95 / p99* (đường `p95` vượt vạch đỏ 1 s) |
| C | `LLM_MODEL=mock-llm-large` | `LLMCostBurnRateHigh` (`for: 2m`, ngưỡng `> $2/giờ`) | **`~135–150s`** (`pending` ngay trong `< 45s` khi cost burn rate 1m vọt lên **`$21.69/giờ`** và 5m lên **`$5.69/giờ`**, `LLM p95` tăng lên `1722.9 ms`; alert cũng đã được quan sát ở trạng thái `firing` trước đó ở mức `$4.35/giờ` sau 2 phút vượt ngưỡng `$2/giờ`) | *Cost burn rate (USD/giờ)* (vọt vào vùng đỏ), *LLM cost (1h)*, *LLM latency p95 theo model* |
| D | `docker pause k5-day14-observability-agent-2` | `AgentInstanceDown` (`for: 30s`, `up == 0`) | **`45.0s`** (`pending` lúc `+15.0s`, `firing` lúc `+45.0s`; `Instances up` giảm `2 → 1`) | Golden Signals *Instances up* (giảm từ `2 → 1`), *Requests/s theo instance* (instance bị pause tụt về 0) |
| E | `AGENT_VERSION=v1` | **Không có alert nào firing** (`AgentHighErrorRate`, `AgentHighLatencyP95`, `AgentInstanceDown` đều `inactive`; `error rate = 1.19%`) | N/A | Bảng *Phiên bản đang chạy* (đổi `v2 → v1`), *Tokens/s* (giảm mạnh từ `1358/709` xuống `670/357 tok/s`), *Retrieval hit ratio* ngừng tăng |

> **Giải thích thêm về Thí nghiệm D (`docker pause` và lỗi 504 ở Nginx):**
> - Dùng `docker pause` thay vì `docker stop` vì Prometheus phát hiện target qua Docker DNS (`dns_sd_configs`). Khi container bị `stop`, Docker xóa IP khỏi DNS nên Prometheus không còn coi target đó là "down" (không alert). Khi bị `pause`, IP vẫn còn trong DNS nhưng process bị đóng băng không trả lời `/metrics` → `up{job="agent"} == 0` → kích hoạt `AgentInstanceDown`.
> - Trong khi container bị pause, một số request Nginx gửi sang instance đó bị treo quá `proxy_read_timeout 10s` và Nginx trả về **HTTP 504 Gateway Timeout**, nhưng **panel Error rate của agent vẫn xanh** vì request không bao giờ hoàn tất qua middleware của agent để tăng counter `http_requests_total`. Do đó trong production cần bổ sung metric ở tầng load balancer (Nginx/ALB) và synthetic black-box monitoring từ bên ngoài.

**Thí nghiệm E không làm alert nào firing. Bạn sẽ phát hiện loại sự cố này trong production bằng cách nào?**
Observability hạ tầng (4 Golden Signals) chỉ đo **sức khoẻ vận hành** (nhanh, không 5xx, còn sống), hoàn toàn "mù" trước **suy giảm chất lượng nội dung** (trả lời sai, hallucination, mất context RAG). Để phát hiện sự cố kiểu E trong production cần kết hợp:
1. **Offline CI/CD Quality Gate:** chạy `eval/run_eval.py` trên golden dataset trước mọi lần merge/deploy, chặn deploy nếu accuracy tụt hoặc có regression (như mục 3.1).
2. **Online Quality Monitoring:** lấy mẫu (sampling) traffic thật chạy chấm điểm tự động bằng **LLM-as-a-judge** hoặc kiểm tra cấu trúc/faithfulness, xuất metric tùy chỉnh (vd `agent_eval_score`, `agent_retrieval_hit_ratio`) sang Prometheus để đặt alert khi `retrieval_hit_ratio` tụt đột ngột.
3. **User Feedback Signals:** theo dõi tỉ lệ thumbs-down / retry / abandon của người dùng trên cùng phiên hội thoại.
4. **Canary Deployment + Version Tracking:** triển khai bản mới cho 5–10% traffic, so sánh đồng thời metric chất lượng và `agent_info{version=...}` trước khi rollout 100%.

### 4.4 Debug bằng trace (Liên kết Traces ↔ JSON Logs)

#### 1) Trace chậm nhất (Slow Trace — [jaeger-slow-trace.png](screenshots/jaeger-slow-trace.png))
- **`trace_id`:** `88ad808d379b0a5343819edc71fc2497`
- **Tổng thời gian (`POST /ask`):** **`1715.87 ms`** (`1.7s` trên Jaeger UI)
- **Phân rã các span:**
  - `POST /ask`: `1715.87 ms` (100.0%)
  - `agent.run`: `1714.22 ms` (99.90%)
  - `agent.retrieve_context`: `15.56 ms` (0.91%)
  - **`chat mock-llm-small`:** **`1698.50 ms` (`98.99%`)** ← **Span chiếm nhiều thời gian nhất** (nút thắt nằm ở upstream LLM call, không phải code FastAPI hay bước retrieval).
- **Dòng log JSON tương ứng** (`docker compose -p k5-day14-observability logs agent | grep 88ad808d379b0a5343819edc71fc2497`):

```json
{"ts": "2026-11-25T08:12:13.676444+00:00", "level": "INFO", "service": "observable-agent", "instance": "07219bb78efa", "event": "llm_call", "trace_id": "88ad808d379b0a5343819edc71fc2497", "span_id": "1346d6faed29845a", "request_id": "a191987085e8", "model": "mock-llm-small", "agent_version": "v2", "input_tokens": 95, "output_tokens": 55, "cost_usd": 4.7e-05, "llm_ms": 1697.7}
{"ts": "2026-11-25T08:12:13.676971+00:00", "level": "INFO", "service": "observable-agent", "instance": "07219bb78efa", "event": "http_request", "trace_id": "88ad808d379b0a5343819edc71fc2497", "span_id": "1346d6faed29845a", "request_id": "a191987085e8", "method": "POST", "route": "/ask", "status": 200, "ms": 1715.5}
```

#### 2) Trace lỗi (Error Trace — [jaeger-error-trace.png](screenshots/jaeger-error-trace.png))
- **`trace_id`:** `45fd55e26e99b694cbe9b20e3a60abdb`
- **Tổng thời gian (`POST /ask`, HTTP `503`):** **`125.15 ms`**
- **Span lỗi:** `chat mock-llm-small` (`97.13 ms`, `error.type=upstream_unavailable`), chứa event **`exception`**: `utils.mock_llm.LLMError: Upstream LLM error: 503 Service Unavailable (mock)`.
- **Dòng log JSON tương ứng** (`docker compose -p k5-day14-observability logs agent | grep 45fd55e26e99b694cbe9b20e3a60abdb`):

```json
{"ts": "2026-11-25T08:17:13.193650+00:00", "level": "INFO", "service": "observable-agent", "instance": "07219bb78efa", "event": "llm_error", "trace_id": "45fd55e26e99b694cbe9b20e3a60abdb", "span_id": "2e4a9a08286f77c9", "request_id": "8f1dea071b1b", "error": "Upstream LLM error: 503 Service Unavailable (mock)"}
{"ts": "2026-11-25T08:17:13.193944+00:00", "level": "INFO", "service": "observable-agent", "instance": "07219bb78efa", "event": "http_request", "trace_id": "45fd55e26e99b694cbe9b20e3a60abdb", "span_id": "2e4a9a08286f77c9", "request_id": "8f1dea071b1b", "method": "POST", "route": "/ask", "status": 503, "ms": 124.8}
```

---

## 5. Benchmark scaling (`production/`, Locust headless, 100 users, ramp 10, 2 phút)

Nguồn dữ liệu: `benchmark-results/scale_1_stats.csv` và `benchmark-results/scale_3_stats.csv`.

### Bảng chính — Endpoint `POST /ask`

| Số instance | Requests | RPS | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Failures |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 7188 | 60.34 | 236.8 | 190 | 220 | 1700 | 63 (0.88%) |
| 3 | 7185 | 60.32 | 234.0 | 190 | 220 | 1700 | 78 (1.09%) |

### Bảng tổng hợp (`Aggregated` — gồm `POST /ask` + `GET /health`)

| Số instance | Requests | RPS | Avg (ms) | p50 (ms) | p95 (ms) | p99 (ms) | Failures |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 7916 | 66.45 | 215.3 | 190 | 220 | 1700 | 63 (0.80%) |
| 3 | 7878 | 66.14 | 213.7 | 190 | 220 | 1700 | 78 (0.99%) |

**Giải thích kết quả:** Scale từ 1 lên 3 instance **gần như không làm thay đổi p50/p95/p99 (`190 / 220 / 1700 ms`) hay RPS (`~60.3 req/s` trên `/ask`, `~66.1–66.5 req/s` tổng hợp)**. Lý do:
1. **Nút thắt nằm ở upstream LLM latency (I/O-bound), không phải CPU của agent:** mỗi request `/ask` dành ~99% thời gian chờ phản hồi từ LLM (`120–180 ms` bình thường, `+1500 ms` cho 3% slow request — xác nhận trực tiếp qua Jaeger trace ở mục 4.4 và panel *LLM latency p95*). Thêm bản sao agent không thể làm upstream LLM trả lời nhanh hơn.
2. **Tải đầu vào bị giới hạn bởi `wait_time` của Locust:** trong `locustfile.py`, mỗi user có `wait_time = between(0.5, 2)` (trung bình `1.25 s`) cộng thời gian phản hồi `~0.23 s` → chu kỳ `~1.48 s/request`. Với 100 users, tốc độ sinh tải tối đa chỉ khoảng `100 / 1.48 ≈ 67 req/s`. Ở mức tải này, 1 instance FastAPI bất đồng bộ chưa bị bão hoà CPU nên 1 hay 3 instance đều xử lý hết tải mà không xếp hàng (nhưng `avg` trên `GET /health` giảm nhẹ `2.62 → 2.46 ms` và `max` giảm mạnh `46.95 → 8.61 ms` khi chạy 3 instance do giảm tải trên từng event loop).
3. **Giá trị thật sự của scale 3 instance:** mang lại **High Availability (HA)** khi 1 instance gặp sự cố (như thí nghiệm D) và tăng tổng công suất chịu tải khi traffic thật vượt ngưỡng bão hoà của 1 instance (như đã thấy ở `c=100` không có `wait_time` trong mục 2.1). Còn để giảm p95/p99 latency của `/ask`, phải tối ưu tầng LLM: semantic caching, chọn model nhanh hơn, streaming response, timeout + retry/fallback sang provider dự phòng.

---

## 6. Kết luận & Trả lời câu hỏi thảo luận

### 3 điều học được về Benchmark & Observability
1. **Average che giấu tail latency — luôn đo và đặt SLO theo p95/p99:** chỉ 3% request chậm đã đẩy p99 lên `1682.6 ms` trong khi avg chỉ `214.5 ms`; ở 10% slow rate, p95 vọt lên `1699.9 ms` nhưng avg vẫn dưới `400 ms`.
2. **Hiệu năng tốt ≠ chất lượng tốt:** agent v1 nhanh hơn và rẻ hơn mỗi request nhưng chỉ đúng `4.35%` (đắt gấp **12 lần** tính theo `cost / câu đúng`) và không làm đỏ bất kỳ alert hạ tầng nào. Cần kết hợp cả load test, golden-dataset eval quality gate trong CI, và observability 3 trụ cột.
3. **3 trụ cột Metrics – Traces – Logs phối hợp thành vòng lặp debug khép kín:** Prometheus/Grafana phát hiện *có sự cố* (p95/error/cost/instance down) → Jaeger chỉ ra *chậm hay lỗi ở span nào* (`chat mock-llm-small` chiếm `98.99%` hoặc ghi `LLMError`) → `trace_id` giúp grep đúng dòng JSON log chứa chi tiết request/token/error chỉ trong vài giây.

### Metric & Alert nên bổ sung khi đưa lên production thật
- **Tầng Edge / Load Balancer:** Nginx/ALB 5xx (đặc biệt `502`/`504` timeout mà agent không tự đếm được) + synthetic uptime probe từ bên ngoài.
- **Tầng LLM & RAG chuyên sâu:** `llm_time_to_first_token_seconds` (TTFT cho streaming), `agent_retrieval_hit_ratio` (alert khi hit ratio tụt đột ngột → phát hiện lỗi vector DB / nhầm version như Thí nghiệm E), `agent_question_length_chars` (Histogram phát hiện prompt quá dài), tỉ lệ rate-limit (`429`) và cost-guard (`402`) từ Part 6, cũng như tỉ lệ lỗi kết nối Redis (`redis_up == 0`).
- **Tầng Chất lượng & Chi phí:** alert theo ngân sách ngày/tháng và điểm online eval sampling (LLM-as-judge) + tỉ lệ feedback tiêu cực từ người dùng.

### Trả lời nhanh 5 câu hỏi thảo luận (`README.md`)
1. **Dashboard xanh nhưng user phàn nàn trả lời sai:** Observability hạ tầng **không** phát hiện được. Cần thêm offline eval quality gate trong CI, online sampling eval (LLM-as-judge), theo dõi `retrieval_hit_ratio`, và user feedback metrics.
2. **Prometheus pull model & Cloud Run (scale-to-zero):** Pull giúp Prometheus kiểm soát chu kỳ scrape, tự biết target chết (`up == 0`) mà app không cần biết địa chỉ server monitoring. Nhưng trên Cloud Run (scale về 0, không có IP cố định), pull trực tiếp không phù hợp — cần push qua OpenTelemetry Collector / Prometheus Remote Write hoặc dùng Cloud Monitoring managed.
3. **Thêm label `user_id` với 100.000 user:** Bùng nổ **cardinality** (100.000 × số route × số status = hàng triệu time series), làm Prometheus tràn RAM, chậm query và OOM crash. Chỉ dùng label có tập giá trị hữu hạn (`route`, `status`, `model`).
4. **Trace 100% request vs Sampling 10%:** Trace mọi request ở traffic lớn tốn rất nhiều băng thông, CPU serialization và chi phí lưu trữ Jaeger/Tempo. Head-based sampling 10% tiết kiệm chi phí nhưng có thể bỏ sót lỗi hiếm; giải pháp tốt hơn là **tail-based sampling** (giữ 100% trace lỗi hoặc chậm > SLO, và chỉ sample 5–10% trace bình thường).
5. **Alert `for: 0s` vs `for: 30m`:** `for: 0s` gây **alert fatigue** (báo động giả liên tục mỗi khi có spike ngẫu nhiên vài giây). Ngược lại `for: 30m` phát hiện sự cố quá chậm, đốt sạch error budget và làm người dùng chịu lỗi suốt nửa tiếng trước khi on-call được báo.
