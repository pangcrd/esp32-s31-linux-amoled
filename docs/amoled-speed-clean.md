# CO5300: tăng tốc TX PIO và giữ login prompt sạch

Ngày: 2026-11-19. Cơ sở bản vá: remote main
d54b23cfe9910d93b1bd837d11d6c438a204bf61.

Trạng thái: bản vá đã qua kiểm tra trên host. Chưa có phép đo trên ESP32-S31
cho bản vá này; chưa khẳng định full frame dưới 100 ms hoặc hết đường quét.

## Đối chiếu source với log

Đã đọc toàn bộ docs/test_log-2.md, docs/prompt-speed-clean.md, log trước đó
docs/user_test_log.md và docs/amoled-spi-debug.md. docs/test_log.md không có
trong checkout hiện tại; test_log-2.md là log cả ngày được người dùng chỉ định.

Log xác nhận firmware đang chạy xoay phần mềm, framebuffer 502x410, 40 MHz,
một cửa sổ cho mỗi vùng dirty. Tuy nhiên blob co5300fb.c trên remote main là
95ad38c1ba375e943df663d37581a75f64079d69: vẫn co5300fb_row(), MADCTL 0x60;
DTS vẫn 20 MHz. Tên commit không phản ánh nội dung các file này. Chưa nhận
được source chính xác của firmware WSL2 đã chạy thành công.

Vì vậy patch against main phải tái lập phép xoay và single-window được mô tả
trong log, rồi tối ưu đường SPI. Không dùng MADCTL để đổi trục. Đây là giữ
kết luận đã chứng minh, không thử lại reset/init/window/hardware.

Ánh xạ dùng trong patch:

    logical (x,y) -> GRAM (22 + 409 - y, x)

Với offsets trong DTS, tổng quát là
(y-offset + height - 1 - y, x-offset + x).
Payload duyệt X tăng dần, mỗi cột duyệt Y từ dưới lên; RGB565 MSB trước.
Full frame: CASET 22..431, RASET 0..501, 411640 byte.
Dirty rows 0..399: CASET 32..431, RASET 0..501, 401600 byte.
CASET 22..421 ghi ở log không khớp phép xoay này. Full frame có thể đúng
nhưng partial update lệch 10 cột, để lại chữ cũ. Patch và test kiểm tra riêng
cửa sổ partial, không chỉ hình full frame.

## Nút thắt và thay đổi cụ thể

Một full frame có 411640 byte. QSPI bốn lane, 40 MHz:

    411640 * 8 / (4 * 40000000) = 20.582 ms
    ceil(411640 / 64) = 6432 FIFO transactions

82.3 ms là tính cho một lane. 20.582 ms chỉ là thời gian bits; không gồm
nạp FIFO, UPDATE, khoảng hở giữa transactions, transpose và scheduling.

spi-esp32s31.c trước patch gọi esp32s31_spi_wait_transaction() mỗi FIFO:
synchronize_irq, completion, UPDATE poll sleep_us=1, IRQ wait, USR poll
sleep_us=1. Linux 7.3.4 dùng usleep_range trong polling có sleep khác 0.
Ở tốc độ này 64 byte quad chỉ mất 3.2 us; chờ IRQ và ngủ có thể lớn hơn
thời gian bits nhiều lần. Đổi row message thành một message lớn vẫn giữ
6432 lần gọi wait, nên chưa loại bỏ nút thắt.

Patch:

- TX-only PIO từ 4 MHz trở lên dùng polling không ngủ. Tắt/synchronize IRQ
  một lần mỗi transfer, không mỗi FIFO.
- Giữ UPDATE -> RAW.TRANS_DONE -> CMD.USR idle; clear completion trước USR.
  Không chỉ đọc USR=0 ngay sau khi start, vì bit có thể chưa kịp lên.
  Timeout thực dựa trên timekeeping; vẫn reset state machine khi timeout.
- Giữ đường IRQ cho tốc độ thấp/RX, cùng đường DMA/target hiện có.
  pio_poll cho phép quay lại đường IRQ để so sánh, không rebuild.
- PIO max_transfer_size từ 4 KiB thành 512 KiB, đủ full frame và geometry
  tối đa 512x512 RGB565. DMA vẫn 4 KiB bounce buffer; target không DMA vẫn
  64 byte. PIO RX cũng nhận giới hạn lớn hơn, nhưng không đổi thuật toán RX.
- Copy từng word 4 byte với kích thước cố định; tail ngắn được zero-pad.
  cond_resched mỗi khoảng 4 KiB; không giữ CPU/IRQ toàn cục suốt một frame.
- Framebuffer vẫn cùng driver/deferred-I/O, gom dirty rows thành một window.
  Xoay theo tiles 16x16 để đọc source gần nhau trong cache.
  Căn vùng dirty gồm hàng lân cận để start và extent của CASET đều chẵn.
- mmap chỉ gom các trang được chạm vào, hợp với damage fbcon; không ép mọi
  lần mmap thành full frame.
- Buffer pixels dùng vzalloc, tránh allocation vật lý liên tục cỡ 412 KiB.
  GPSPI PIO truy cập trực tiếp buffer; controller không có can_dma, nên
  __spi_map_msg trong SPI core không DMA-map buffer này.
- Giữ framing: CASET/RASET và tham số một lane; 32 00 2c 00 một lane,
  pixels bốn lane, cùng message/CS. Giữ reset raw, supplier init, delays
  150 ms/600 ms và framebuffer RAM đã chứng minh.
- Bỏ DIAG per-row; thêm flush_stats sysfs, đọc không printk và không redraw.
  Timing chứa thời gian scheduling trong flush, không chứa delay gom dirty
  của deferred I/O (khoảng 40 ms).

Chưa port GDMA: chưa có số liệu chứng minh sau khi bỏ IRQ/sleep thì DMA
vẫn bắt buộc. DMA không thay phép xoay phần mềm. Nếu full_spi_us vẫn vượt
ngân sách, số liệu này sẽ định hướng bước tiếp theo thay vì đoán.
Do panel quét độc lập và PIO vẫn có 6432 transaction, dưới 100 ms cũng
không tự chứng minh hết tearing/đường quét; cần xác nhận quan sát trên board.

## Login prompt sạch và tồn tại sau reboot

Đây là thay đổi rootfs, không thể chỉ flash kernel. post-build.sh nhận biết
linux-amoled.fragment, cài /sbin/amoled-getty, thay tty0 getty bằng:

    tty0::respawn:/sbin/amoled-getty

Wrapper xoá màn hình trước khi exec getty. PID 1 vẫn khởi chạy/respawn getty,
giữ cách khởi chạy đã hoạt động; serial getty vẫn như cấu hình đang dùng.
Wrapper chạy lại mỗi lần logout/respawn, nên prompt mới luôn được in sau
clear. Không clear sau khi getty đã in prompt.

Kernel messages xuất hiện muộn sau khi getty chạy vẫn có thể ghi lên tty0;
patch không thay console routing/loglevel. Phép kiểm tra reboot bên dưới cần
xác nhận màn hình sạch trong boot thực tế, không chỉ thứ tự script.

## Kiểm tra đã chạy

- Compile hai object với Linux 7.3.4 thực tế, W=1: PASS, không cảnh báo.
  Đây là compile-test trên host x86_64; chưa build toàn bộ image RISC-V.
- DTS qua C preprocessor và dtc của cùng tree: PASS, không cảnh báo.
- checkpatch --strict cho diff hai driver: 0 errors, 0 warnings, 0 checks.
- check-co5300-windows.py compile code thật với ASan/UBSan: PASS; kiểm tra
  full/partial/odd/bottom/mmap, tile edges 2x2/502x410/512x512, từng byte RGB565,
  mixed-lane CS, retry cả window và stats.
- check-esp32s31-pio.py compile các hàm thật với fake MMIO, ASan/UBSan: PASS;
  single/quad, aligned/unaligned/tail/full 411640 byte, clear stale RAW,
  UPDATE -> RAW -> USR, timeout/reset, synchronize_irq một lần, legacy/DMA/
  target selection và limits.
- Wrapper/post-build: shell syntax, chạy lặp hai lần, profile AMOLED và
  headless, đúng một tty0 line, clear trước prompt: PASS.
- git apply --check rồi git apply thật trong clone mới của main: PASS.
  git diff --check và hai test host chạy trên source đã apply: PASS.

Fake MMIO chỉ kiểm tra logic phần mềm; không chứng minh timing của silicon
hoặc tốc độ panel. Board test không cần Python, pkill hay SPI reads.

## WSL2: lấy patch, build và flash theo thứ tự

Các lệnh dưới dùng checkout sạch ở ~/s31-linux, output AMOLED riêng và
bootloader artifacts đã dùng trong lần flash thành công trước đó.
Nếu source WSL2 đang có sửa đổi chưa commit, lưu chúng trước khi đổi branch;
patch dưới được kiểm tra against main nêu trên.

    cd ~/s31-linux
    git fetch origin
    git switch -c test/co5300-speed-clean origin/main
    curl --fail --location \
      https://raw.githubusercontent.com/pangcrd/esp32-s31-linux-amoled/fix/co5300-speed-clean/patches/co5300-speed-clean.patch \
      -o /tmp/co5300-speed-clean.patch
    git apply --check /tmp/co5300-speed-clean.patch
    git apply /tmp/co5300-speed-clean.patch

    git submodule update --init --recursive
    python3 scripts/check-co5300-windows.py linux/drivers/video/fbdev/co5300fb.c
    python3 scripts/check-esp32s31-pio.py linux/drivers/spi/spi-esp32s31.c

    export ESP32S31_VERSION="$(make -s version)"
    BR_OUT="$PWD/build/br-output-coreboard1-amoled"
    if [ ! -f "$BR_OUT/.config" ]; then
      make -C external/buildroot O="$BR_OUT" \
        BR2_EXTERNAL="$PWD/br2-external" coreboard1_defconfig
    fi
    sh scripts/enable-amoled.sh "$BR_OUT"
    make -C external/buildroot O="$BR_OUT" BR2_EXTERNAL="$PWD/br2-external"

    sh scripts/copy-images.sh "$BR_OUT/images" build/coreboard1 \
      bootloader/partitions-coreboard1.csv
    make -s flash-args BOARD=coreboard1 > build/coreboard1/flash-args.txt
    source ~/esp/esp-idf/export.sh
    python -m esptool --chip esp32s31 merge-bin \
      -o build/coreboard1/flash.bin @build/coreboard1/flash-args.txt

    FLASH_PORT=/dev/ttyACM0
    python -m esptool --chip esp32s31 --port "$FLASH_PORT" --baud 921600 \
      write-flash 0x0 build/coreboard1/flash.bin

Đặt FLASH_PORT thành port flash đang dùng. scripts/enable-amoled.sh chạy
olddefconfig và linux-dirclean; kernel được extract/copy overlay lại.
Không dùng scripts/ci-build.sh hoặc áp lại coreboard1_defconfig sau khi
enable AMOLED, vì sẽ làm mất fragment opt-in.

Nếu lấy branch fix/co5300-speed-clean trực tiếp thì source đã có patch;
không chạy git apply lần nữa. Lệnh build/flash từ export ESP32S31_VERSION
trở xuống giống hệt.

## USB rootfs đang dùng: cập nhật nhỏ, giữ dữ liệu USB

Build mới đã cài wrapper vào rootfs.ext2/sdcard.img, được copy vào
build/coreboard1/buildroot/. Nếu cài USB image mới bằng cách đang dùng thì
không cần chỉnh tay.

Với USB hiện tại, có thể cập nhật đúng hai file này qua serial shell trên
board, rồi sync/reboot; thay đổi nằm trên rootfs USB nên tồn tại sau reboot.
Không cần ghi lại cả USB:

    cat > /sbin/amoled-getty <<'EOF'
    #!/bin/sh
    printf '\033[0m\033[2J\033[H\033[3J' > /dev/tty0
    exec /sbin/getty -L tty0 115200 vt100
    EOF
    chmod 755 /sbin/amoled-getty
    sed -i '/^tty0::respawn:/d' /etc/inittab
    printf '%s\n' 'tty0::respawn:/sbin/amoled-getty' >> /etc/inittab
    sync
    reboot

Đừng chạy clear riêng sau reboot: wrapper đảm nhiệm clear trước prompt.

## Board: xác minh và đo full frame

Chạy từ serial hoặc SSH, không từ login VT trên panel:

    dmesg | grep -E 'co5300|UPDATE timeout|TRANS_DONE timeout|USR timeout'
    cat /sys/class/graphics/fb0/virtual_size
    grep '^tty0::respawn:' /etc/inittab
    cat /sys/module/spi_esp32s31/parameters/pio_poll
    cat /sys/bus/spi/devices/*/flush_stats

Kỳ vọng: virtual_size 502,410; probe có "(tiled single-window)" và 40000000 Hz;
tty0 gọi /sbin/amoled-getty; pio_poll=Y; không timeout; errors=0.
Sau reboot, log kernel hiển thị theo hướng đã xác nhận, rồi màn hình được
xoá và còn login prompt mới; prompt không biến mất do clear sau getty.

Để có 5 mẫu full frame giữ nguyên ảnh/prompt, từ WSL2 chép test script sang
board theo địa chỉ IP hiện tại (log trước dùng 192.168.0.182):

    scp scripts/test-amoled-speed.sh root@192.168.0.182:/tmp/test-amoled-speed.sh

Sau đó ở serial/SSH trên board:

    sh /tmp/test-amoled-speed.sh

Script snapshot RAM của /dev/fb0, rồi ghi lại đúng 411640 byte năm lần.
Không đọc panel qua SPI. Nó giữ ảnh prompt thay vì đổ zero rồi làm mất prompt.
Có 1 s giữa các mẫu và xoá file snapshot khi kết thúc.

Đọc full_render_us/full_spi_us/full_total_us cho full frame gần nhất,
không dùng total_us của cursor/partial update. full_flushes phải tăng qua
các mẫu, errors giữ 0. Đích full_total_us < 100000, càng thấp càng tốt;
max_full_us có thể gồm một frame lúc boot bị scheduler trì hoãn.

Nếu cần A/B với đường IRQ cũ, chỉ đổi module parameter:

    printf 'N\n' > /sys/module/spi_esp32s31/parameters/pio_poll
    sh /tmp/test-amoled-speed.sh
    printf 'Y\n' > /sys/module/spi_esp32s31/parameters/pio_poll

Đường N có thể chậm hơn nhiều. Nó vẫn giữ rotation/window/rootfs mới;
A/B chỉ cô lập tác động của TX polling. Sau reboot mặc định là Y.
Nếu bản Y có timeout, giữ log timeout và stats; N là fallback phần mềm.

Gửi lại output 5 mẫu Y và mô tả panel có còn quét chậm/chữ cũ không.
full_spi_us lớn -> còn chi phí PIO/refill/scheduling; full_render_us lớn ->
còn chi phí transpose/cache; lỗi partial/clean prompt cần đối chiếu vùng dirty
và thời điểm getty. Không cần thêm phép đo điện hay thử đọc SPI.

## Cấu hình panel khác

Giữ mục này cho bước sau, đúng thứ tự ưu tiên yêu cầu. GPIO, compatible,
frequency và init-sequence hiện đã ở DTS. Built-in DTB cần rebuild/flash
khi sửa DTS. Rotation software và QSPI framing của CO5300 không thể áp
nguyên sang ST7789/ILI9341; cơ chế cấu hình nhiều panel cần được thiết kế
sau khi số liệu speed và prompt trên board xác nhận hai việc hiện tại.
