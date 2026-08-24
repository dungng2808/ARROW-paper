"# Workspace ARROW

Khoá workspace này chứa pipeline ARROW để phân tích repository, sinh test bằng LLM và chạy build Maven/Gradle tự động.

## Khởi tạo Git và kết nối repository

Repository GitHub: [dungng2808/ARROW](https://github.com/dungng2808/ARROW)

Mở terminal tại thư mục `workspace`, sau đó chạy lần lượt:

```bash
# 1. Khởi tạo Git trong thư mục hiện tại
git init

# 2. Kết nối với repository trên GitHub
git remote add origin https://github.com/dungng2808/ARROW-paper

# 3. Đặt tên nhánh chính là main
git branch -M main

# 4. Thêm và commit toàn bộ mã nguồn
git add .
git commit -m "Initial commit"

# 5. Đẩy mã nguồn lên GitHub
git push -u origin main
```

Chỉ cần chạy `git init` và `git remote add origin` một lần. Nếu remote `origin` đã tồn tại nhưng chưa đúng địa chỉ, cập nhật bằng:

```bash
git remote set-url origin https://github.com/dungng2808/ARROW.git
```

## Chuẩn bị dữ liệu classes2test

Thư mục classes2test không được cung cấp sẵn trong repo này. Bạn có thể lấy bộ dữ liệu từ repository chính thức:

- GitHub: [lopsandrea/classes2test](https://github.com/lopsandrea/classes2test)
- Tải ZIP trực tiếp: [classes2test-main.zip](https://github.com/lopsandrea/classes2test/archive/refs/heads/main.zip)

Để clone trực tiếp vào workspace với đúng tên thư mục, chạy:

```bash
git clone https://github.com/lopsandrea/classes2test.git
```

Nếu bạn tải về dưới dạng zip hoặc folder khác, hãy giải nén và đổi tên thư mục thành:

```text
classes2test
```

Pipeline sẽ đọc dữ liệu từ thư mục này để chạy. Nếu thư mục không tồn tại hoặc tên khác, chương trình sẽ không hoạt động đúng như mong đợi



