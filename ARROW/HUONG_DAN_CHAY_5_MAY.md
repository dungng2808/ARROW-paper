# Hướng dẫn chọn 200 sample sạch trên 1 Mac và 4 Windows

Tài liệu này dùng để năm máy chạy qualification cùng lúc, lấy đúng 200 sample
chính và 50 sample dự phòng. Bước này không gọi LLM và không dùng kết quả sinh
test để lựa chọn sample.

## 1. Phân công máy

| Máy | Hệ điều hành | Shard index |
|---|---|---:|
| Máy điều phối | macOS | 0 |
| Máy Windows 1 | Windows | 1 |
| Máy Windows 2 | Windows | 2 |
| Máy Windows 3 | Windows | 3 |
| Máy Windows 4 | Windows | 4 |

Các máy chạy shard 0-4 song song. Không cần chờ máy trước hoàn thành. Chỉ bước
merge cuối cùng phải đợi đủ kết quả của cả năm máy.

## 2. Quy tắc chung

- Không copy hoặc commit thư mục `Java-version`, `.venv`, `runs` hay `repos`.
- Mỗi máy tự cài JDK phù hợp với hệ điều hành; JDK macOS không chạy trên Windows.
- Không sửa `candidate_manifest.csv` hoặc các JSON trong mini dataset.
- Cả năm máy phải dùng cùng commit trên nhánh `main`.
- Mỗi máy phải dùng đúng một shard index khác nhau từ 0 đến 4.
- Khi một máy bị dừng, chạy lại đúng lệnh, run ID và output directory để resume.
- Không dùng `--no-resume` khi tiếp tục một run đang dở.
- Không cần API key ở bước chọn sample vì script không gọi LLM.

Candidate đã được khóa tại:

```text
shards/clean-samples-seed42/candidate_manifest.csv
```

SHA256 bắt buộc:

```text
7972bccb046154036617af5776debe75c973209c6921308fdb5bc47637921c1f
```

## 3. Chuẩn bị máy Windows

### 3.1. Cài JDK 8, 11, 17 và 21

Mở PowerShell bằng quyền **Run as Administrator**, sau đó chạy:

```powershell
winget install -e --id EclipseAdoptium.Temurin.8.JDK  --accept-package-agreements --accept-source-agreements
winget install -e --id EclipseAdoptium.Temurin.11.JDK --accept-package-agreements --accept-source-agreements
winget install -e --id EclipseAdoptium.Temurin.17.JDK --accept-package-agreements --accept-source-agreements
winget install -e --id EclipseAdoptium.Temurin.21.JDK --accept-package-agreements --accept-source-agreements
```

Đóng PowerShell, mở lại rồi kiểm tra cả bốn JDK:

```powershell
Get-ChildItem "C:\Program Files\Eclipse Adoptium" -Directory | ForEach-Object {
    Write-Host "=== $($_.FullName) ==="
    & "$($_.FullName)\bin\java.exe" -version
    & "$($_.FullName)\bin\javac.exe" -version
}
```

Phải nhìn thấy đủ major version 8, 11, 17 và 21. ARROW tự tìm JDK trong
`C:\Program Files\Eclipse Adoptium`; không cần copy JDK vào repository và không
cần đặt một `JAVA_HOME` chung cho cả bốn phiên bản.

### 3.2. Kiểm tra công cụ

Máy phải có Git, Python 3.11+ và Maven. Gradle project ưu tiên Gradle Wrapper.
Kiểm tra:

```powershell
git --version
py -3 --version
mvn -version
```

Nếu một lệnh chưa tồn tại, cài công cụ đó và mở PowerShell mới trước khi tiếp tục.

### 3.3. Clone và cài Python dependencies

Thực hiện một lần trên mỗi máy Windows:

```powershell
git clone https://github.com/dungng2808/ARROW-paper.git
Set-Location ARROW-paper
git switch main
git pull --ff-only origin main

Set-Location ARROW
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Nếu đã clone repository thì không clone lại; chỉ chạy:

```powershell
Set-Location ARROW-paper
git switch main
git pull --ff-only origin main
Set-Location ARROW
```

### 3.4. Kiểm tra commit và candidate checksum

```powershell
git log -1 --oneline
git status --short
Get-FileHash .\shards\clean-samples-seed42\candidate_manifest.csv -Algorithm SHA256
```

Gửi kết quả `git log -1 --oneline` vào nhóm. Năm máy chỉ bắt đầu khi commit
giống nhau. `git status --short` phải không in ra thay đổi tracked nào trước khi
chạy. Hash candidate phải đúng giá trị SHA256 ghi ở phần 2.

## 4. Chạy trên bốn máy Windows

Mỗi máy đặt `$ShardIndex` theo bảng ở phần 1. Ví dụ máy Windows 1 dùng index 1:

```powershell
$ShardIndex = 1

.\.venv\Scripts\python.exe select_clean_samples.py `
  --dataset shards/clean-samples-seed42/dataset `
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv `
  --run-id "clean-200-shard-$ShardIndex" `
  --output-dir "runs/sample_selection/distributed/shard-$ShardIndex" `
  --target 200 `
  --reserve 50 `
  --seed 42 `
  --shard-count 5 `
  --shard-index $ShardIndex `
  --workers 2 `
  --batch-size 25 `
  --baseline-repeats 2
```

Ba máy Windows còn lại thay `$ShardIndex` lần lượt bằng 2, 3 và 4. Máy ít RAM
có thể đổi `--workers 2` thành `--workers 1`; không thay các tham số lựa chọn khác.

Khi bị gián đoạn, đặt lại đúng `$ShardIndex` rồi chạy lại nguyên lệnh trên. Audit
được checkpoint theo batch và script sẽ bỏ qua candidate đã hoàn thành.

## 5. Chạy trên máy Mac điều phối

Máy Mac hiện dùng shard index 0. Cài đủ Temurin JDK 8, 11, 17 và 21 bằng
Homebrew; các JDK được cài vào hệ thống, không nằm trong repository:

```bash
brew install --cask temurin@8 temurin@11 temurin@17 temurin@21
```

Kiểm tra phải thấy đủ bốn major version:

```bash
/usr/libexec/java_home -V
```

Khai báo đường dẫn cho ARROW trong terminal sẽ chạy qualification:

```bash
export JAVA_8_HOME="$(/usr/libexec/java_home -v 1.8)"
export JAVA_11_HOME="$(/usr/libexec/java_home -v 11)"
export JAVA_17_HOME="$(/usr/libexec/java_home -v 17)"
export JAVA_21_HOME="$(/usr/libexec/java_home -v 21)"

"${JAVA_8_HOME}/bin/java" -version
"${JAVA_11_HOME}/bin/java" -version
"${JAVA_17_HOME}/bin/java" -version
"${JAVA_21_HOME}/bin/java" -version
```

Kiểm tra commit và checksum:

```bash
cd ARROW-paper
git switch main
git pull --ff-only origin main
git log -1 --oneline
git status --short

cd ARROW
openssl dgst -sha256 shards/clean-samples-seed42/candidate_manifest.csv
```

Chuẩn bị môi trường nếu chưa có:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
```

Chạy shard 0 cùng lúc với bốn máy Windows:

```bash
# Nếu vừa mở terminal mới, khai báo lại bốn biến JAVA_*_HOME ở trên.
SHARD_INDEX=0

./.venv/bin/python select_clean_samples.py \
  --dataset shards/clean-samples-seed42/dataset \
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv \
  --run-id clean-200-shard-${SHARD_INDEX} \
  --output-dir runs/sample_selection/distributed/shard-${SHARD_INDEX} \
  --target 200 \
  --reserve 50 \
  --seed 42 \
  --shard-count 5 \
  --shard-index ${SHARD_INDEX} \
  --workers 2 \
  --batch-size 25 \
  --baseline-repeats 2
```

## 6. Gửi kết quả của từng máy lên main

Chỉ gửi hai file `preflight_audit.csv` và `provenance.json`. Không commit toàn bộ
`runs`, repo clone, log build hoặc JDK.

Trên Windows, sau khi shard chạy xong:

```powershell
$ShardIndex = 1  # đổi theo máy

New-Item -ItemType Directory -Force "shard-results/shard-$ShardIndex" | Out-Null
Copy-Item "runs/sample_selection/distributed/shard-$ShardIndex/preflight_audit.csv" "shard-results/shard-$ShardIndex/"
Copy-Item "runs/sample_selection/distributed/shard-$ShardIndex/provenance.json" "shard-results/shard-$ShardIndex/"

Set-Location ..
git add -- "ARROW/shard-results/shard-$ShardIndex/preflight_audit.csv" "ARROW/shard-results/shard-$ShardIndex/provenance.json"
git commit -m "data: add clean sample shard $ShardIndex"
git pull --rebase origin main
git push origin HEAD:main
```

Trên máy Mac, sau khi shard 0 chạy xong:

```bash
SHARD_INDEX=0
mkdir -p shard-results/shard-${SHARD_INDEX}
cp runs/sample_selection/distributed/shard-${SHARD_INDEX}/preflight_audit.csv shard-results/shard-${SHARD_INDEX}/
cp runs/sample_selection/distributed/shard-${SHARD_INDEX}/provenance.json shard-results/shard-${SHARD_INDEX}/

cd ..
git add -- ARROW/shard-results/shard-${SHARD_INDEX}/preflight_audit.csv ARROW/shard-results/shard-${SHARD_INDEX}/provenance.json
git commit -m "data: add clean sample shard ${SHARD_INDEX}"
git pull --rebase origin main
git push origin HEAD:main
```

Nếu push bị từ chối vì máy khác vừa push trước, chạy lại:

```bash
git pull --rebase origin main
git push origin HEAD:main
```

Mỗi máy chỉ được commit thư mục shard của chính mình.

## 7. Merge trên máy điều phối

Đợi main có đủ `shard-results/shard-0` đến `shard-results/shard-4`, sau đó:

```bash
git pull --ff-only origin main
cd ARROW

./.venv/bin/python select_clean_samples.py \
  --candidate-manifest shards/clean-samples-seed42/candidate_manifest.csv \
  --run-id clean-200-merged-seed42 \
  --output-dir runs/sample_selection/clean-200-merged-seed42 \
  --target 200 \
  --reserve 50 \
  --merge-shard-dir shard-results/shard-0 \
  --merge-shard-dir shard-results/shard-1 \
  --merge-shard-dir shard-results/shard-2 \
  --merge-shard-dir shard-results/shard-3 \
  --merge-shard-dir shard-results/shard-4
```

Merge sẽ từ chối khi thiếu shard, trùng index, candidate nằm sai shard hoặc
candidate checksum không khớp.

Các file quan trọng được tạo trong
`runs/sample_selection/clean-200-merged-seed42`:

```text
final_manifest_200.csv
reserve_manifest_50.csv
selection_summary.json
final_manifest_200_shard_0_of_5.csv
final_manifest_200_shard_1_of_5.csv
final_manifest_200_shard_2_of_5.csv
final_manifest_200_shard_3_of_5.csv
final_manifest_200_shard_4_of_5.csv
```

Mở `selection_summary.json` và xác nhận `enough_samples` là `true`. Nếu chưa đủ
200 + 50, không tự chọn thêm dựa trên generated-test result; phải tạo candidate
pool mới lớn hơn theo cùng quy trình khóa seed.

## 8. Các lỗi cần tránh

- Hai máy dùng cùng shard index.
- Một máy sửa candidate CSV/JSON hoặc dùng commit khác các máy còn lại.
- Chỉ cài một JDK 17 rồi dùng nó cho mọi repository Java cũ.
- Chạy khi `mvn`, Git hoặc Python chưa hoạt động.
- Xóa `runs/sample_selection/distributed/shard-i` khi shard chưa hoàn thành.
- Commit `ARROW/repos`, `ARROW/runs`, `.venv`, `Java-version` hoặc API key.
- Dùng kết quả test sinh bởi LLM để quyết định giữ hay loại sample.
