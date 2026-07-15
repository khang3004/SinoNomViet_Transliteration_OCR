# hvb-processing

Pipeline OCR Châu bản triều Nguyễn (Hán Nôm ↔ Quốc ngữ) trên **K3s + Airflow + MinIO + Qdrant**.

Phiên bản hiện tại: **v2.2 — TOC/STT + refine/stitch/confidence**.

Đơn vị lưu trữ nghiệp vụ là **STT** (số thứ tự mục trong sách), không phải “một trang = một đề tài”.

---

## `page_kind` là gì?

`page_kind` bảo OCR biết **loại trang** đang xử lý để chọn đúng prompt + schema JSON.

| Giá trị | Nghĩa | Dùng khi |
|---------|--------|----------|
| **`toc`** | *Table of Contents* = **mục lục / trích yếu** | Trang có nhiều mục `1.` `2.` `3.`, mỗi mục có Ngày / Tờ-Tập / Loại / Đề tài + khối TRÍCH YẾU 2 cột |
| **`body`** | Trang **thân văn** (chiếu/sắc đầy đủ) | Một (hoặc ít) văn bản chạy dài; schema cũ `de_tai` + `blocks` |

### Vì sao phải tách?

Trang mục lục (vd. scan page 49) thường có **nhiều đề tài** trên cùng một trang.  
Nếu OCR kiểu `body` (một field `de_tai` / trang), model hay **gộp 2–3 đề tài thành 1** → Qdrant cũng sai.

Với `page_kind=toc`, output là:

```json
{
  "page_type": "muc_luc",
  "page_header": "MỤC LỤC CHÂU BẢN TRIỀU NGUYỄN",
  "printed_page": 3,
  "entries": [
    { "stt": 1, "de_tai": "...", "trich_yeu": { "han_nom": "...", "quoc_ngu": "..." } },
    { "stt": 2, "de_tai": "...", "trich_yeu": { "han_nom": "...", "quoc_ngu": "..." } }
  ]
}
```

- `page_header` — tiêu đề đầu trang  
- `printed_page` — số in ở chân trang (khác `page_no` = số file scan)  
- `entries[]` — mỗi STT một đề tài riêng  

Alias tương đương `toc`: `muc_luc`, `catalog` (env `HVB_PAGE_KIND`).

Smoke hiện tại **49–58** → luôn dùng **`page_kind=toc`** (mặc định pipeline).

---

## Luồng hiện tại (v2.2)

```text
preprocess → OCR(toc) → build_catalog → refine → stitch → index_catalog
```

- **confidence / flags** trên mỗi `stt_*.json` (`ocr_confidence`, `refine_confidence`, `flags`)
- **refine** (DeepSeek): chỉ entry conf thấp / có cờ
- **stitch**: nối entry cắt trang / trích yếu rỗng bằng context trang kế
- **index**: bỏ entry rác / conf < `index_min_confidence` (mặc định 0.45)

---

## Thứ tự chạy DAG

### A. Lần đầu / sau khi đổi schema TOC

```text
1. hvb_cleanup_toc_state_pipeline   # đã chạy sẵn khi migrate v2.2
2. hvb_v2_full_pipeline             # preprocess→OCR→catalog→refine→stitch→index
```

Hoặc từng bước:

```text
1. hvb_opencv_preprocess_pages_pipeline
2. hvb_ocr_v2_pages_pipeline          # page_kind=toc
3. hvb_build_catalog_pipeline
4. hvb_refine_entries_pipeline
5. hvb_stitch_entries_pipeline
6. hvb_index_catalog_qdrant_pipeline
```

### B. Re-OCR một vài trang (vd. 52)

```text
1. hvb_ocr_v2_pages_pipeline          # force=true, pages="52", page_kind=toc
2. hvb_build_catalog_pipeline
3. hvb_refine_entries_pipeline
4. hvb_stitch_entries_pipeline
5. hvb_index_catalog_qdrant_pipeline
```

### C. Full pipeline (một nút)

DAG: **`hvb_v2_full_pipeline`**

```
preprocess → OCR(toc) → catalog → refine → stitch → index
```

Ví dụ conf:

```json
{
  "doc_id": "hvb_base",
  "pages": "49-58",
  "page_kind": "toc",
  "force": false,
  "qdrant_recreate": false
}
```

| Param | Ý nghĩa |
|-------|---------|
| `doc_id` | Tài liệu (`hvb_base`) |
| `pages` | `49-58`, `52`, `1,3,5` — trống = theo manifest |
| `page_kind` | `toc` (mục lục) hoặc `body` (thân văn) |
| `force` | `true` = gọi lại API dù MinIO đã có artifact |
| `qdrant_recreate` | `true` = xóa/tạo lại collection pairs trước khi index |

---

## State / skip API

Nếu object đã có trên MinIO → **không gọi lại** preprocess / OCR / align (tiết kiệm quota).

| Muốn | Cách |
|------|------|
| Chạy tiếp bình thường | `force=false` (mặc định) |
| Re-OCR / re-preprocess | `force=true` hoặc env `HVB_FORCE=true` |
| Xóa state sóng TOC rồi làm lại | `hvb_cleanup_toc_state_pipeline` |

---

## MinIO buckets (v2.1)

| Bucket | Nội dung |
|--------|----------|
| `hvb-raw` | PDF split + manifest |
| `hvb-preprocessed` | PNG sau OpenCV `{doc}/page_XXXX.png` |
| `hvb-ocr` | JSON OCR trang (TOC: `entries[]`; body: `blocks`) |
| `hvb-aligned` | JSON align (chỉ **body**) |
| `hvb-catalog` | `{doc}/catalog.json` — danh sách STT |
| `hvb-entries` | `{doc}/stt_NNNN.json` — entry theo số thứ tự |
| `hvb-layout` | Dự phòng YOLO (chưa dùng) |

---

## Qdrant

- Collection: `hvb_chau_ban_pairs`
- TOC: **1 point / cặp trích yếu của 1 STT**
- Payload quan trọng: `stt`, `entry_id`, `de_tai`, `page_no`, `printed_page`, `page_type`

---

## Danh sách DAG

| DAG | Vai trò |
|-----|---------|
| `hvb_pdf_split_pipeline` | Tách PDF → pages + manifest |
| `hvb_cleanup_toc_state_pipeline` | Dọn OCR/aligned/catalog/entries + recreate Qdrant |
| `hvb_opencv_preprocess_pages_pipeline` | PDF page → PNG denoised |
| `hvb_ocr_v2_pages_pipeline` | OCR Gemini (`page_kind=toc\|body`) |
| `hvb_build_catalog_pipeline` | TOC OCR → catalog + `stt_*.json` (+ flags/confidence) |
| `hvb_refine_entries_pipeline` | DeepSeek refine entry yếu / có cờ |
| `hvb_stitch_entries_pipeline` | Nối cắt trang bằng OCR trang kế |
| `hvb_index_catalog_qdrant_pipeline` | Entries sạch → Qdrant |
| `hvb_v2_full_pipeline` | TOC end-to-end (kèm refine+stitch) |
| `hvb_align_v2_pages_pipeline` | Align DeepSeek (**body only**) |
| `hvb_index_pairs_qdrant_pipeline` | Index aligned pages (**body only**) |

> Align / index_pairs **bỏ qua** trang `page_type=muc_luc` — dùng `build_catalog` + `index_catalog` thay thế.

---

## Cấu trúc repo

```text
hvb-processing/
├── dags/
│   ├── config.ini              # runtime (không commit secret)
│   ├── deploy_airflow.sh
│   ├── jobs/                   # runners + common/
│   └── pipelines/              # Airflow DAG .py (phẳng, không subfolder)
└── scripts/                    # upload / init Qdrant / secrets
```

Deploy code lên Airflow:

```bash
export KUBECONFIG=~/.kube/config.k3s-new   # nếu cần
bash dags/deploy_airflow.sh upload
```

DAGs sync từ MinIO: `airflow/dags/hvb-processing/`.

---

## Setup nhanh

```bash
# 1. Config + secret API (Ramclouds / Gemini)
cp dags/config.ini.example dags/config.ini   # nếu có example
export GEMINI_OPENCV_API_KEY='sk-...'
bash scripts/setup_ocr_secrets.sh

# 2. Upload PDF nguồn + split (một lần)
bash scripts/upload_hvb_base.sh
# Airflow: hvb_pdf_split_pipeline

# 3. Deploy DAG
bash dags/deploy_airflow.sh upload

# 4. Airflow: hvb_v2_full_pipeline
# conf: {"doc_id":"hvb_base","pages":"49-58","page_kind":"toc"}
```

Config chính `[gemini_opencv]`, `[align]`, `[qdrant]`, `[pipeline]`:

- `default_page_kind = toc`
- `force_reprocess = false`
- OCR model mặc định: `gemini-3.5-flash-low` (Ramclouds)
- Align (body): `deepseek-v4-flash`

Env ghi đè: `HVB_GEMINI_OPENCV_API_KEY`, `HVB_PAGE_KIND`, `HVB_FORCE`, `HVB_QDRANT_RECREATE`, …

---

## Entry schema (tóm tắt)

`hvb-entries/hvb_base/stt_0001.json`:

```json
{
  "entry_id": "hvb_base_stt_0001",
  "stt": 1,
  "chi_muc": {
    "ngay_thang": "...",
    "to_tap": "1/1",
    "the_loai": "Chiếu",
    "xuat_xu": "Đại Nội",
    "de_tai": "Tập hợp con cháu trong họ."
  },
  "page_header": "MỤC LỤC CHÂU BẢN TRIỀU NGUYỄN",
  "trich_yeu": { "han_nom": "...", "quoc_ngu": "..." },
  "content_alignment": [ /* cặp từ trích yếu; body bổ sung sau */ ],
  "source_pages": [{ "page_no": 49, "printed_page": 3, "page_type": "muc_luc" }],
  "status": "catalog_only"
}
```

---

## Roadmap ngắn

| Wave | Việc |
|------|------|
| **A (đang làm)** | TOC `entries[]` → catalog theo STT → Qdrant |
| **B** | OCR/align thân văn + assemble vào `stt_*.json` |
| **C** | Stitch cắt trang + context trang trước (cùng STT) |
| **D** | YOLO layout (sau khi annotate) |

---

## Git / secrets

- Commit: code DAG, `config.ini.example`, scripts, k8s manifests  
- Không commit: `config.ini` có key thật, kubeconfig, output OCR local  
- API key → K8s Secret qua `scripts/setup_ocr_secrets.sh`
