# Share pack — S31 Linux trên CO5300 AMOLED

Cặp firmware trong checklist này là **PIO đã test trên board**:
fix/co5300-speed-clean @ ab6cd0be1ff52c1baac86ac93629af7b88e7a33b.
Ngày 2026-11-19: median 87667 us/frame, 3/5 mẫu dưới 100 ms, errors=0;
vạch quét vẫn nhẹ. Bản DMA ở fix/co5300-gdma cần test riêng, xem
[co5300-gdma.md](co5300-gdma.md).

## 0. Source trước khi share

Đã kiểm tra remote: main và fix/co5300-speed-clean đều ở ab6cd0b.
Bản PIO đã vào main; bước merge đã đạt, không cần tạo merge lặp.

Kiểm tra lại trên WSL trước khi đóng gói:

~~~bash
cd ~/s31-linux
git fetch origin
git merge-base --is-ancestor ab6cd0be1ff52c1baac86ac93629af7b88e7a33b origin/main
~~~

Exit 0 chứng minh main chứa revision PIO. Link source trong mô tả dùng
commit cố định để vẫn khớp cặp binary khi main tiếp tục phát triển.
Nếu sau này cần merge và gặp conflict, dừng merge và gửi log cho Rumi
theo yêu cầu ban đầu; không force-push.

## 1. Hai file và kiểm tra cặp binary

Đường dẫn bản đã build trong WSL:

~~~bash
ls -lh build/coreboard1/flash.bin build/coreboard1/buildroot/sdcard.img
~~~

- flash.bin: kernel/loader, flash tại 0x0.
- sdcard.img: USB rootfs, có /sbin/amoled-getty và đúng một getty tty0.
- Board root trên USB; người nhận cần cả hai file.

**Trạng thái trong server workspace khi làm checklist:** hai đường dẫn trên
đều chưa có file; GitHub Releases chưa có asset. Vì vậy chưa kiểm tra checksum
hay đóng gói binary thật, chưa upload Forge/Releases. Đã hỏi người dùng về
đường dẫn hoặc link binary. Không dùng số đo PIO làm kết quả DMA.

Helper scripts/prepare-amoled-share.py được cung cấp trong patch/nhánh DMA.
Chạy nó trước build DMA ghi đè output PIO cũ. Nếu còn ở main PIO, tải riêng
helper vào scripts/:

~~~bash
curl --fail --location \
  https://raw.githubusercontent.com/pangcrd/esp32-s31-linux-amoled/fix/co5300-gdma/scripts/prepare-amoled-share.py \
  -o scripts/prepare-amoled-share.py
python3 scripts/prepare-amoled-share.py \
  --kernel-build build/br-output-coreboard1-amoled/build/linux-7.3.4 \
  --flash build/coreboard1/flash.bin \
  --sdcard build/coreboard1/buildroot/sdcard.img \
  --output build/share-co5300-pio
~~~

Chạy trên WSL host (Python/debugfs/e2fsprogs), không trên ESP32. Helper kiểm tra:

- Các source kernel/SPI/framebuffer/clock/DT trong build tree khớp ab6cd0b.
- Config AMOLED, kernel Image header RISC-V, và Image nằm trong flash.bin.
- MBR/PARTUUID USB, nội dung wrapper/mode 0755 và một tty0 wrapper getty.
- SHA256 hai file copy vào pack; không ghi đè artifact khác.

Đây là kiểm tra consistency của build artifacts; helper không boot/test board
hay chứng minh một build tái lập bit-for-bit. Metadata os-release được lưu
nguyên trong manifest để xem phiên bản rootfs.

Output:

~~~text
build/share-co5300-pio/
  flash.bin
  sdcard.img
  SHA256SUMS
  manifest.json
  forge-description.txt
~~~

Helper đã qua kiểm tra với fixture ext4/MBR tổng hợp trên host, bao gồm việc
từ chối source sai, kernel DMA hoặc flash không chứa Image. Fixture không phải
binary firmware để phát hành.

## 2. Đăng ESP Forge và USB image

ESP Forge nhận một .bin để flash ở 0x0: upload flash.bin.
Đặt sdcard.img ở download riêng, ví dụ GitHub Releases/Google Drive.
Hai file cần cùng pack và SHA256SUMS; thêm link USB vào mô tả trước khi đăng.

Sau khi có link chính xác của sdcard.img, tạo lại mô tả/manifest có link:

~~~bash
python3 scripts/prepare-amoled-share.py \
  --kernel-build build/br-output-coreboard1-amoled/build/linux-7.3.4 \
  --flash build/coreboard1/flash.bin \
  --sdcard build/coreboard1/buildroot/sdcard.img \
  --output build/share-co5300-pio \
  --usb-url 'LINK_TAI_SDCARD_IMG'
cat build/share-co5300-pio/forge-description.txt
~~~

Không upload mô tả còn [LINK_SDCARD_IMG]. Template ghi rõ PIO khoảng 87 ms,
còn vạch quét nhẹ, DMA đang phát triển; source pin vào commit đã test.
Việc upload chưa được thực hiện vì binary/link chưa có trong workspace.

## 3. Người nhận

1. Flash flash.bin tại 0x0 qua Forge/esptool.
2. Ghi sdcard.img ra USB bằng balenaEtcher, cắm USB-A, cấp nguồn.

Boot log kernel và login prompt hiện trên panel. SSH chỉ nhận key; người nhận
thêm public key của họ vào /root/.ssh/authorized_keys qua serial.

## DMA và tiêu chí phát hành tiếp theo

Patch DMA không đổi getty/rootfs, nên board đang dùng rootfs PIO có thể
test với flash.bin DMA. Acceptance DMA vẫn cần 5 mẫu thật và quan sát panel:
cả 5 full_total_us dưới 100000, errors=0, không timeout, scroll/prompt đúng.
Chưa ghi nhận acceptance DMA đạt và chưa chuẩn bị binary DMA để chia sẻ.
