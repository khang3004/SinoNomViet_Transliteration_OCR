# hvb-processing

OCR pipeline: chạy nhiều model để compare hiệu suất, deploy DAG lên Airflow qua MinIO.

## Structure

```text
hvb-processing/
├── data/
│   ├── raw/          # PDF input (local dev)
│   └── output/       # JSON OCR output (local dev)
├── dags/
│   ├── config.ini.example   # template config (commit được)
│   ├── config.ini           # config thật (không commit — xem .gitignore)
│   ├── deploy_airflow.sh
│   ├── jobs/                # OCR logic + từng model
│   └── pipelines/           # Airflow DAGs (file .py phẳng — KHÔNG đặt trong subfolder)
│       ├── pdf_split_pipeline.py
│       ├── ocr_paddle_pipeline.py
│       ├── ocr_kandianguji_pipeline.py
│       ├── ocr_google_vision_pipeline.py
│       ├── ocr_chatgpt_pipeline.py
│       ├── ocr_gemini_pipeline.py
│       └── ocr_compare_pipeline.py
└── scripts/
    ├── upload_hvb_base.sh
    ├── read_ocr_results.sh
    ├── deploy_paddle_ocr.sh
    ├── setup_ocr_secrets.sh
    └── deploy.sh
├── services/
│   └── paddle_ocr/          # PaddleOCR microservice (FastAPI)
└── k8s/
    └── paddle-ocr.yaml
```

## `config.ini.example` là gì?

Đây là **file mẫu** để bạn tạo config thật:

```bash
cp dags/config.ini.example dags/config.ini
# rồi sửa access_key, endpoint... theo môi trường của bạn
```

| File | Commit git? | Mục đích |
|------|-------------|----------|
| `config.ini.example` | Có | Template, không chứa secret thật — chia sẻ trong team |
| `config.ini` | Không | Config chạy thật trên máy/pod Airflow |

Khi deploy, `config.ini` được upload lên MinIO cùng DAG code để pod Airflow đọc được.

## Quick Start

```bash
cp dags/config.ini.example dags/config.ini
pip install -r dags/requirements.txt
# bỏ PDF vào data/raw/ rồi đồng bộ lên MinIO source
bash scripts/upload_hvb_base.sh          # chỉ upload source PDFs -> hvb-raw/hvb_base/source
bash scripts/run_split.sh                 # upload source + split từ MinIO -> pages + manifest
bash scripts/run_batch.sh paddle          # 1 model
bash scripts/run_local.sh                 # compare tất cả model
```

## Deploy Airflow

```text
MinIO:  airflow/dags/hvb-processing/
Pod:    /opt/airflow/dags/hvb-processing/
```

```bash
bash dags/deploy_airflow.sh upload
# hoặc
bash scripts/deploy.sh
```

Script `deploy_airflow.sh` mirror toàn bộ `dags/` lên MinIO và **xóa DAG cũ** không còn trong repo (ví dụ `hvb_ocr_batch_pipeline`).

## Airflow DAGs

| DAG | Mô tả |
|-----|--------|
| `hvb_pdf_split_pipeline` | Đọc PDF từ `hvb-raw/hvb_base/source`, tách trang và ghi `pages + manifest` |
| `hvb_ocr_paddle_pages_pipeline` | OCR Paddle từng trang → `ocr/paddle/{doc_id}/page_XXXX.json` (K8s pod) |
| `hvb_ocr_gemini_pages_pipeline` | OCR Gemini từng trang → `ocr/gemini/{doc_id}/page_XXXX.json` (K8s pod) |
| `hvb_index_qdrant_pipeline` | Index JSON từ MinIO → Qdrant (`model_folder`: `paddle` hoặc `gemini`) |
| `hvb_ocr_paddle_pipeline` | OCR Paddle (legacy batch, 1 file/doc) |
| `hvb_ocr_kandianguji_pipeline` | OCR với KanDianGuJi |
| `hvb_ocr_google_vision_pipeline` | OCR với Google Vision |
| `hvb_ocr_chatgpt_pipeline` | OCR với ChatGPT Vision |
| `hvb_ocr_gemini_pipeline` | OCR Gemini (legacy batch, 1 file/doc) |
| `hvb_ocr_compare_pipeline` | (Tuỳ chọn) Compare nhiều model cùng lúc |

Mỗi model có **file pipeline riêng** trong `dags/pipelines/` (cùng cấp với `pdf_split_pipeline.py`).

> **Lưu ý:** Airflow cluster này **không quét DAG trong subfolder** `pipelines/ocr/`. Không tạo `pipelines/__init__.py`.

### Hybrid Paddle + Gemini (khuyến nghị)

1. **Paddle** — thân bản Hán Nôm: `hvb_ocr_paddle_pages_pipeline`
2. **Gemini** — mục lục/metadata Latin: `hvb_ocr_gemini_pages_pipeline`
3. **Index** — chạy `hvb_index_qdrant_pipeline` hai lần với `model_folder` khác nhau:

```json
{"doc_id":"hvb_base","model_folder":"paddle","pages":"11-1216"}
{"doc_id":"hvb_base","model_folder":"gemini","pages":"1-10"}
```

MinIO paths: `ocr/paddle/...` và `ocr/gemini/...` — cùng collection Qdrant, point ID tách theo `model_name`.

### Trigger trên Airflow UI

1. `hvb_pdf_split_pipeline` — tách trang
2. Một trong các DAG OCR, ví dụ `hvb_ocr_gemini_pipeline`
3. Param `upload_minio` (mặc định `true`) nếu form hiện khi Trigger

**Chọn trang / tài liệu** (params khi Trigger):

| Param | Ví dụ | Ý nghĩa |
|-------|-------|---------|
| `doc_id` | `hvb_base` | Chỉ OCR 1 tài liệu |
| `pages` | `1` | Chỉ trang 1 |
| `pages` | `1,3,5` | Các trang cụ thể |
| `pages` | `1-10` | Trang 1 đến 10 |
| (trống) | | Tất cả trang |

JSON kết quả khi chọn trang: `hvb_base_gemini_p1-3.json` (không ghi đè file full).

Trigger w/ config ví dụ:
```json
{"doc_id": "hvb_base", "pages": "1-5", "upload_minio": true}
```

Compare nhiều model: `hvb_ocr_compare_pipeline` với `models=paddle,gemini,...`

## OCR Models (đã tích hợp)

| Model | Cách chạy trên K3s | Cần chuẩn bị |
|-------|-------------------|--------------|
| Gemini | Gọi API từ Airflow pod | `gemini.api_key` hoặc `HVB_GEMINI_API_KEY` |
| ChatGPT | Gọi API từ Airflow pod | `openai.api_key` hoặc `HVB_OPENAI_API_KEY` |
| Google Vision | Gọi API từ Airflow pod | GCP service account JSON |
| PaddleOCR | Microservice riêng `hvb-paddle-ocr` | Deploy `scripts/deploy_paddle_ocr.sh` |
| KanDianGuJi | HTTP API tương thích `POST /ocr` | `kandianguji.service_url` + `api_key` |

### Setup lần đầu

```bash
# 1. Cập nhật config (copy section mới từ config.ini.example)
cp dags/config.ini.example dags/config.ini   # nếu chưa có
# điền api_key / service_url trong config.ini

# 2. Cài dependencies trên Airflow (restart scheduler)
bash dags/patch_airflow_hvb_requirements.sh

# 3. Deploy DAG code + config.ini lên MinIO
bash dags/deploy_airflow.sh upload

# 4. (Tuỳ chọn) Deploy PaddleOCR service — không cần Docker trên Mac:
bash scripts/deploy_paddle_ocr_k3s.sh          # CPU, 1 replica
bash scripts/deploy_paddle_ocr_k3s.sh --gpu    # GPU, 2 replicas (load-balance 2 node)
# Hoặc build image Docker (cần Docker Desktop): bash scripts/deploy_paddle_ocr.sh

# 5. Tạo K8s secret cho Gemini (bắt buộc cho gemini_pages pipeline)
export GEMINI_API_KEY=your-key
bash scripts/setup_ocr_secrets.sh
```

### Chạy pipeline

1. `bash scripts/upload_hvb_base.sh`
2. Airflow: `hvb_pdf_split_pipeline`
3. Airflow: `hvb_ocr_gemini_pipeline` (hoặc model khác)
4. `bash scripts/read_ocr_results.sh download` → đọc JSON trong `data/output/`

### Đọc kết quả OCR

```bash
bash scripts/read_ocr_results.sh list
bash scripts/read_ocr_results.sh cat hvb_base_gemini.json
bash scripts/read_ocr_results.sh download
```

## Config (`dags/config.ini`)

| Section | Keys |
|---------|------|
| `[paths]` | `raw_dir`, `staging_dir`, `output_dir` |
| `[minio]` | endpoint, credentials, buckets, prefixes |
| `[gemini]` | `api_key`, `model` |
| `[openai]` | `api_key`, `model` |
| `[google_vision]` | `credentials_json` (hoặc env `GOOGLE_APPLICATION_CREDENTIALS`) |
| `[paddle]` | `service_url` (mặc định `http://hvb-paddle-ocr.ocr.svc.cluster.local:8080`) |
| `[kandianguji]` | `service_url`, `api_key` |

Biến môi trường ghi đè config: `HVB_GEMINI_API_KEY`, `HVB_OPENAI_API_KEY`, ...

## Git policy for K3s deploy and secrets

### Nên commit lên git

- `k8s/*.yaml` (manifest deploy)
- `scripts/deploy_*.sh`, `dags/deploy_airflow.sh` (runbook/deploy script)
- `dags/config.ini.example` (template không chứa secret thật)

Lý do: K3s đang chạy chỉ là runtime state; repo cần là source of truth để tái tạo môi trường.

### Không commit lên git

- `dags/config.ini`, `dags/config.k3s-new`
- Bất kỳ file chứa API key/token/password
- OCR output local, logs, temp files (`scripts/data/output/`, `*.log`, `tmp/`, ...)

### API key có nên mã hóa trong repo không?

Không nên lưu API key trong repo (kể cả mã hóa thủ công). Cách đúng:

1. Lưu key ở môi trường runtime: K8s Secret hoặc env vars.
2. Commit duy nhất file template (`config.ini.example`) với giá trị rỗng/placeholder.
3. Dùng `scripts/setup_ocr_secrets.sh` để tạo/update secret trong cluster.

Nếu bắt buộc chia sẻ secret trong git nội bộ, dùng công cụ quản lý secret chuyên dụng (`SOPS`, `Sealed Secrets`, `Vault`) thay vì tự mã hóa.

### Nếu đã lỡ commit key thật

1. Rotate/revoke key ngay ở nhà cung cấp.
2. Xóa key khỏi code/config và cập nhật `.gitignore`.
3. Rewrite git history để loại key khỏi commit cũ (`git filter-repo` hoặc BFG), sau đó push lại theo quy trình team.
