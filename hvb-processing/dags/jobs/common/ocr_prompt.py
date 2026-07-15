# TOC / mục lục: nhiều entry theo STT trên cùng trang / Mục lục: nhiều entry theo STT
HVB_OCR_TOC_PROMPT = """Bạn là chuyên gia OCR mục lục Châu bản triều Nguyễn.
Ảnh là trang MỤC LỤC / trích yếu: có NHIỀU mục đánh số 1. 2. 3. ... trên cùng trang.
KHÔNG dịch, KHÔNG thêm bớt, KHÔNG gộp các mục thành một.

Trả về ĐÚNG một JSON (không markdown):
{
  "page_type": "muc_luc",
  "page_header": "tiêu đề đầu trang (vd: MỤC LỤC CHÂU BẢN TRIỀU NGUYỄN) hoặc null",
  "printed_page": số_trang_in_chân_trang_hoặc_null,
  "orphan_head": {
    "han_nom": "nếu ĐẦU trang chỉ còn đoạn TRÍCH YẾU body tiếp nối (chưa có số N. / chưa có Tờ-Tập-Đề tài mới) — giữ \\n; ngược lại rỗng",
    "quoc_ngu": "..."
  },
  "entry_continuation": {
    "ngay_thang": "null hoặc phần Ngày còn thiếu của STT trang trước",
    "to_tap": "...",
    "the_loai": "...",
    "xuat_xu": "...",
    "de_tai": "...",
    "trich_yeu": {"han_nom": "...", "quoc_ngu": "..."}
  },
  "entries": [
    {
      "stt": 1,
      "ngay_thang": "...",
      "to_tap": "...",
      "the_loai": "Chiếu|Chỉ Dụ|Sắc|...",
      "xuat_xu": "...",
      "de_tai": "...",
      "trich_yeu": {
        "han_nom": "cột Hán — GIỮ '\\n' đúng từng dòng trên ảnh",
        "quoc_ngu": "cột Quốc ngữ — GIỮ '\\n' đúng từng dòng trên ảnh"
      }
    }
  ]
}

Quy tắc BẮT BUỘC:
1. Mỗi số thứ tự "N." ở đầu khối = MỘT entry riêng; KHÔNG được gộp 2 đề tài vào 1 entry.
2. Trường "Đề tài:" của entry nào chỉ thuộc entry đó.
3. Khối sau dòng "TRÍCH YẾU": đọc THEO CỘT — trái = han_nom, phải = quoc_ngu; không đọc ngang trộn hai cột.
4. XUỐNG DÒNG: mỗi dòng chữ mắt thường thấy trên ảnh phải là một dòng trong chuỗi (dùng \\n). KHÔNG gộp thành một đoạn liền.
5. Giữ đúng nhãn Ngày / Tờ/Tập / Loại / Xuất xứ / Đề tài như trên ảnh.
6. printed_page = số in ở chân trang (vd trang này là 3), không phải số file PDF/scan.
7. page_header = dòng tiêu đề lớn đầu trang nếu có.
8. Nếu thiếu field metadata thì null, nhưng vẫn tách đủ số entry theo số "N." nhìn thấy trên ảnh.
9. KHÔNG viết chữ "None" / "null" vào text; để chuỗi rỗng nếu không đọc được.
10. Nếu entry bị cắt ở cuối trang: vẫn ghi MỌI phần đọc được trên ảnh; không bịa phần chưa thấy.
11. ƯU TIÊN CUỐI TRANG: mục STT cuối — đọc HẾT mọi dòng còn hiện (Ngày, Tờ/Tập, Loại, Xuất xứ, Đề tài, TRÍCH YẾU nếu có). CẤM dừng sớm chỉ lấy dòng Ngày rồi bỏ các dòng bên dưới/bên cạnh vẫn nhìn thấy.
12. Nếu trên ảnh CÒN chữ Hán/Quốc ngữ của khối TRÍCH YẾU (không chỉ nhãn) → CẤM để cả hai cột rỗng. NHƯNG nếu trang KẾT THÚC đúng dòng nhãn "TRÍCH YẾU" (không còn dòng chữ body bên dưới) → trich_yeu PHẢI để rỗng {"han_nom":"","quoc_ngu":""}; CẤM copy Đề tài / bịa nội dung; phần body sẽ nằm orphan_head trang sau.
13. orphan_head: nếu trang MỞ ĐẦU bằng đoạn chữ Hán/Việt TRÍCH YẾU (tiếp nối) TRƯỚC metadata/STT mới → điền orphan_head, KHÔNG gộp vào entry STT đầu trang.
14. entry_continuation (cắt giữa entry, bất kỳ vị trí nào trong metadata→TY):
   Ví dụ trang N cuối: "11." + Ngày + Tờ/Tập (+ có thể Loại) rồi hết trang.
   Trang N+1 đầu (sau tiêu đề trang): Xuất xứ + Đề tài + TRÍCH YẾU, KHÔNG có số "11." mới.
   → Trang N: ghi vào entries[] những field đã thấy; field chưa thấy = null.
   → Trang N+1: phần không có số STT mới → entry_continuation (không tạo stt giả, không nhét vào STT kế).
   Chỉ bắt đầu entries[] từ số "N." nhìn thấy rõ trên ảnh.
15. Cắt trang có thể ở: chỉ Ngày | Ngày+vài dòng metadata | tới nửa Đề tài | tới nhãn/nửa TY — cùng một cơ chế entry_continuation + stitch merge field-by-field.
16. Nếu cuối trang chỉ còn "N." + Ngày (và thật sự không còn dòng khác trên ảnh) → field còn lại = null là đúng. Nếu còn dòng khác mà để null = SAI.
"""


# Focused OCR for last entry at page bottom / OCR tập trung mục cuối trang bị cắt
HVB_OCR_TOC_BOTTOM_PROMPT = """Bạn là chuyên gia OCR mục lục Châu bản. Ảnh thường là PHẦN DƯỚI trang mục lục.
Chỉ OCR MỤC CUỐI cùng (STT lớn nhất) còn hiện trên ảnh — kể cả khi bị cắt nửa câu.

Trả về ĐÚNG một JSON (không markdown):
{
  "page_type": "muc_luc",
  "printed_page": số_hoặc_null,
  "entries": [
    {
      "stt": số_STT_nhìn_thấy,
      "ngay_thang": "...",
      "to_tap": "...",
      "the_loai": "...",
      "xuat_xu": "...",
      "de_tai": "...",
      "trich_yeu": {
        "han_nom": "cột Hán — giữ \\n theo dòng",
        "quoc_ngu": "cột Quốc ngữ — giữ \\n theo dòng"
      }
    }
  ]
}

Quy tắc:
- Đọc THEO CỘT (trái Hán, phải Việt), giữ \\n.
- Đọc HẾT mọi dòng còn hiện của mục cuối: Ngày, Tờ/Tập, Loại, Xuất xứ, Đề tài, TRÍCH YẾU nếu có. CẤM chỉ lấy Ngày rồi bỏ các dòng khác vẫn thấy trên ảnh.
- Phải điền trich_yeu nếu còn chữ body; không để cả hai rỗng khi ảnh còn chữ Hán/Việt của TRÍCH YẾU.
- Nếu ảnh chỉ còn nhãn "TRÍCH YẾU" (không body) → để trich_yeu rỗng; không bịa.
- Không bịa phần không thấy trên ảnh.
- Không viết "None".
"""

# Body page: keep visual line breaks / Trang thân văn: giữ xuống dòng như ảnh
HVB_OCR_STRUCTURED_PROMPT = """Bạn là chuyên gia OCR Châu bản triều Nguyễn (Hán Nôm + Quốc ngữ).
Đọc toàn bộ chữ trên ảnh. KHÔNG dịch, KHÔNG thêm bớt.

Trả về ĐÚNG một JSON (không markdown) với schema:
{
  "page_header": "chuỗi hoặc null (tiêu đề đầu trang nếu có)",
  "printed_page": "số trang in ở chân trang (int hoặc null)",
  "ngay_thang": "chuỗi hoặc null",
  "the_loai": "chuỗi hoặc null (Chiếu/Sắc/Tấu/...)",
  "de_tai": "chuỗi hoặc null",
  "blocks": [
    {"script": "han_nom|quoc_ngu|mixed", "text": "..."}
  ]
}

Quy tắc:
- Mỗi đoạn/câu liền mạch là một block.
- Script "han_nom" nếu chủ yếu chữ Hán/Nôm; "quoc_ngu" nếu tiếng Việt Latin; "mixed" nếu lẫn.
- Giữ nguyên dấu câu, số thứ tự; mỗi dòng trên ảnh = một \\n trong text. KHÔNG gộp dòng.
- printed_page là số in trong sách (chân trang), không phải số file scan.
- Nếu không đọc được trường metadata thì để null (không viết chữ "None").
"""
