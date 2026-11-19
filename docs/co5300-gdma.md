# CO5300: AXI GDMA cho GPSPI2

Cơ sở patch: fix/co5300-speed-clean @
ab6cd0be1ff52c1baac86ac93629af7b88e7a33b.
Ngày: 2026-11-19. DMA mới đã qua kiểm tra trên host, chưa chạy trên board.

## Kết luận từ log và source

Đã dùng docs/gdma-dma.md và log được người dùng cung cấp. docs/test_log.md
không có trong workspace; đã đọc docs/test_log-2.md, docs/user_test_log.md và
docs/amoled-spi-debug.md ở lượt trước.

PIO đã được người dùng đo: full_total_us 124003, 82680, 87667, 170281, 82431;
median 87667 us, render khoảng 14.8 ms, errors=0. Đây là kết quả của PIO ab6cd0b,
không phải số đo của patch DMA. Không thử lại reset/init/wiring/đọc panel.

Linux 7.3.4 và overlay hiện không có provider GDMA cho S31. Donor GrieferPig
có esp32s31-axi-gdma.c và esp32s31-ahb-gdma.c; đã kiểm tra donor, không giả định
rằng phần cứng không có DMA.

TRM và ESP-IDF thống nhất GPSPI2 dùng **AXI GDMA**, selector 1. AHB GDMA không
kết nối GPSPI2. TRM có 3 TX + 3 RX độc lập; chỉ memory-to-memory mới cần ghép
TX/RX cùng số. Driver mới cung cấp 6 directional channel cho slave-SG,
không bật memcpy/memset/cyclic chưa cần cho panel.

SPI branch hiện có bounce DMA, nhưng không có dmas trên node và không có
provider, nên luôn PIO. Có DMA sẽ đổi max_transfer_size thành 4 KiB; full
frame bị SPI core chia khoảng 101 transfers, mỗi transfer phải copy/sync,
arm DMA rồi chờ hai completion.

Một giới hạn quan trọng khác: **MS_DATA_BITLEN chỉ có 18 bit**, tính theo
số bit trừ một. Tối đa 262144 bit = 32768 byte. Không thể chỉ tăng buffer
hay ghi 411640 * 8 - 1 vào MS_DLEN cho một transaction.

## Thay đổi

- Thêm AXI GDMA dmaengine provider, register layout/slave-SG model đối chiếu
  donor và TRM. Mỗi RX/TX half có vchan, IRQ và lifecycle độc lập.
- Descriptor ba word, padding thành 16 byte để địa chỉ 8-byte aligned.
  Dùng dma_pool trong coherent SRAM pool sẵn có; payload có thể ở PSRAM.
  Mỗi descriptor tối đa 4032 byte (<= 4095, multiple của cache line 64).
  Owner được bật; chỉ descriptor TX cuối có suc_eof. RX length/eof khởi tạo 0.
- Claim peripheral được serialize; không cho hai channel cùng direction
  nối cùng peripheral. RX0/TX0 dùng chung SPI2 selector 1 là hợp lệ.
- Bật owner check, descriptor burst, 32-byte memory burst, TX EOF sau khi
  data được pop. TX hoàn tất ở TOTAL_EOF, không ở từng descriptor DONE.
- Abort/stop rồi chờ RESET_AVAIL trước reset/free descriptor. DMA errors
  thành DMA_ERROR. Khi reset chưa an toàn, giữ descriptor thay vì free nó
  trong khi DMA còn có thể dùng.
- Thêm clock/reset AXI GDMA vào provider clock hiện có; CLK_IGNORE_UNUSED
  giữ clock cho LCD firmware tùy chọn khi Linux GDMA không được enable. Thêm DT node
  0x20348000/0x2000, IRQ rx0/tx0/rx1/tx1/rx2/tx2. Node mặc định disabled;
  chỉ AMOLED DTS enable và thêm dmas/dma-names tx/rx.
- AMOLED fragment bật DMADEVICES và ESP32S31_AXI_GDMA; các dependencies
  DMA_ENGINE/DMA_VIRTUAL_CHANNELS do Kconfig select.
- Host TX-only dùng SPI core SG mapping trực tiếp từ buffer pixels vmalloc.
  Không copy cả pixels vào bounce buffer. SPI core map/sync PSRAM cache
  theo DMA device của channel; không tự trộn physical/virtual addresses.
- Host max_transfer_size 32 KiB, đúng MS_DLEN. SPI core tự chia:
  12 x 32768 + 18424 = 411640 byte, tổng 13 transaction DMA.
  Vẫn một CASET/RASET window, một message và một GPIO-CS assertion.
  Header 32 00 2c 00 vẫn một lane; mọi pixel transfer vẫn quad, cùng thứ tự.
- Giữ DMA bounce RX/duplex/target 4 KiB hiện có. Host RX/duplex lớn hơn
  bounce buffer dùng PIO, tránh overflow khi cap host tăng lên 32 KiB.
  Không sửa hoặc đề xuất kiểm tra lại SPI panel-read path.
- UPDATE/USR polling ở helper IRQ không ngủ từng microsecond; phần truyền
  dài vẫn chờ IRQ/completion, để CPU làm việc khác thay vì nạp FIFO.
- Giữ pio_poll. Thêm dma_enable=Y mặc định; N chọn PIO để fallback/A-B.
  Transfer đã SG-map vẫn hoàn thành bằng DMA nếu parameter đổi giữa message.
- Thêm dma_stats của controller: available/enabled, số TX SG transfers,
  bytes và errors. Chỉ tăng counters thành công sau cả DMA và GPSPI idle.
  Kiểm tra DMA status/AFIFO errors để không trả 0 khi DMA lỗi.
- co5300fb.c, wrapper getty và post-build.sh giữ nguyên từng byte so với ab6cd0b.
  flush_stats, landscape 502x410, 40 MHz, init và các delays vẫn như bản đã test.

PIO fallback và cap 32 KiB với channel đã được cấp vẫn giữ message/CS. Panel
dùng GPIO-CS. Với native-CS, patch giữ KEEP_ACTIVE nếu còn transfer trong
message; không deassert giữa các chunk DMA.

## Register/API evidence và giới hạn tài liệu

TRM chính thức đã đọc: ESP32-S31 Technical Reference Manual, Pre-release v0.2:
https://documentation.espressif.com/esp32-s31_technical_reference_manual_en.pdf

| Phần | Nguồn đối chiếu |
| --- | --- |
| AXI GDMA, 3 TX + 3 RX, PSRAM support | TRM p97–99 |
| Descriptor fields, owner/eof, 8-byte alignment | TRM p100–101 |
| SPI2 selector 1, peripheral exclusivity | TRM p101–102, table 3.4-3 |
| IRQ events, reset/clock procedure | TRM p110–112 |
| RX/TX register offsets, stride 0x68 | TRM p125–128 |
| CONF0/CONF1, burst, owner-check, LINK1/LINK2 | TRM p206–216 |
| RESET_AVAIL 0x28c/0x298, MISC 0x2a8 | TRM p220–221 |
| Internal/external memory windows, base 0x20348000 size 8 KiB | TRM p319, table 5.3-2 |

ESP-IDF pinned revision:
fff9895c82d744c7237be8847347bdd1b07c6643.
Files: components/esp_hal_dma/esp32s31/include/hal/{gdma_ll,axi_dma_ll,gdma_channel}.h,
components/soc/esp32s31/register/soc/{axi_dma_reg,hp_sys_clkrst_reg,spi_reg}.h,
components/soc/esp32s31/include/soc/interrupts.h,
components/esp_hal_gpspi/esp32s31/include/hal/spi_ll.h.

Đã kiểm tra APM trong bootloader/main/main.c và opensbi/platform/esp32s31/platform.c:
SRAM pool/PSRAM thông thường được mở, vùng firmware M-mode vẫn được bảo vệ.
Không thêm thao tác đổi quyền APM hay thay bootloader/OpenSBI cho DMA này.

Donor pinned:
https://github.com/GrieferPig/linux-esp32-s31/blob/2bf05bc5f379066cf5f64d750afbb0b4ba0bf364/drivers/dma/esp32s31-axi-gdma.c

**TRM v0.2 chưa xuất bản chương SPI, Reset/Clock và Interrupt Matrix.**
Vì vậy không thể khẳng định mọi offset của các phần đó đã được đối chiếu TRM:
HP_SYS_CLKRST_AXI_PDMA_CTRL0 offset 0x78, gate bit 0/reset bit 1/FORCE_NORST
bit 2; IRQ source IDs RX 48/49/50, TX 51/52/53; SPI MS_DLEN 0x1c, field
17:0 và FIFO-reset sequence được đối chiếu các file ESP-IDF pinned ở trên.
Đã hỏi người dùng về TRM đầy đủ hơn. Đây là giới hạn nguồn tài liệu công khai,
không phải bằng chứng GDMA bị phần cứng chặn.

API được kiểm tra trong Linux 7.3.4 thực tế:
virt-dma.h/dmaengine.h, dma_pool, devm_clk_get_enabled, reset assert/deassert,
of_dma_controller_register, dma_get_slave_channel, slave SG, cookie status,
termination/synchronization, atomic64 và device dev_groups.
SPI core spi.c tự split theo max_transfer_size, map vmalloc theo trang và
sync SG trước transfer; metadata lanes/CS được giữ qua splitting.

## Kết quả kiểm tra host

- GDMA, SPI, clock object compile với Linux 7.3.4, W=1: PASS.
  Host x86_64 compile-test; chưa build/flash cả image RISC-V tại đây.
- check-esp32s31-dma.py với ASan/UBSan: PASS. Code thật cho SG LLIs, tails,
  unaligned/range/512K, allocation failures, IRQ/error/abort, channel claims;
  SPI TX-SG + provider code thật cho 13 transfers/411640 bytes, quad/MS_DLEN,
  errors/cleanup/counters/fallback.
- PIO regression và framebuffer/rotation/window regression: PASS.
- DTS preprocess + dtc: PASS. DTB có GDMA okay, 6 IRQ đúng thứ tự,
  tx=<GDMA TX0 SPI2>/rx=<GDMA RX0 SPI2>, 40 MHz và 502x410.
- Kconfig/Makefile patch áp dụng vào Linux 7.3.4: PASS.
- checkpatch --strict: không lỗi code; cảnh báo MAINTAINERS chuẩn khi thêm
  driver mới được ghi nhận (overlay này không có MAINTAINERS riêng).
- git apply --check và apply thật lên clone mới fix/co5300-speed-clean,
  rồi chạy lại các regression trên source đã apply: PASS.

Fake MMIO không chứng minh DMA trên silicon, bus timing hoặc hết vạch quét.
Nền tảng thời gian lý tưởng là khoảng 14.8 ms render + 20.6 ms bits, còn cache
sync, 13 arm/completion và scheduling. Mục tiêu 30–50 ms là kỳ vọng để đo,
không phải số đo đã đạt. Dưới 100 ms cũng không tự chứng minh hết tearing
do panel quét độc lập.

## WSL2: patch, build, flash

Dùng bootloader artifacts và output AMOLED đã build thành công trước đó.
Có thay đổi kernel config/DT/provider nên phải rebuild kernel qua linux-dirclean.
Không cần rebuild mọi package userspace từ đầu; USB rootfs có getty của bản PIO
có thể dùng tiếp. Buildroot make có thể đóng gói rootfs lại, nhưng bản DMA
không yêu cầu ghi USB mới. Flash phần thay đổi nằm trong flash.bin.

Nếu muốn giữ/share cặp PIO đã test, chạy prepare-amoled-share.py theo checklist
TRƯỚC khi build DMA ghi đè output cũ.

    cd ~/s31-linux
    git fetch origin
    git switch -c test/co5300-gdma origin/fix/co5300-speed-clean
    curl --fail --location \
      https://raw.githubusercontent.com/pangcrd/esp32-s31-linux-amoled/fix/co5300-gdma/patches/co5300-gdma.patch \
      -o /tmp/co5300-gdma.patch
    git apply --check /tmp/co5300-gdma.patch
    git apply /tmp/co5300-gdma.patch

    python3 scripts/check-esp32s31-dma.py \
      linux/drivers/dma/esp32s31-axi-gdma.c linux/drivers/spi/spi-esp32s31.c
    python3 scripts/check-esp32s31-pio.py linux/drivers/spi/spi-esp32s31.c
    python3 scripts/check-co5300-windows.py linux/drivers/video/fbdev/co5300fb.c

    unset ESP32S31_VERSION
    export ESP32S31_VERSION="$(make -s version)"
    BR_OUT="$PWD/build/br-output-coreboard1-amoled"
    sh scripts/enable-amoled.sh "$BR_OUT"
    make -C external/buildroot O="$BR_OUT" BR2_EXTERNAL="$PWD/br2-external"

    grep -E '^CONFIG_(DMADEVICES|DMA_ENGINE|DMA_VIRTUAL_CHANNELS|ESP32S31_AXI_GDMA)=' \
      "$BR_OUT/build/linux-7.3.4/.config"
    sh scripts/copy-images.sh "$BR_OUT/images" build/coreboard1 \
      bootloader/partitions-coreboard1.csv
    make -s flash-args BOARD=coreboard1 > build/coreboard1/flash-args.txt
    source ~/esp/esp-idf/export.sh
    python -m esptool --chip esp32s31 merge-bin \
      -o build/coreboard1/flash.bin @build/coreboard1/flash-args.txt
    FLASH_PORT=/dev/ttyACM0
    python -m esptool --chip esp32s31 --port "$FLASH_PORT" --baud 921600 \
      write-flash 0x0 build/coreboard1/flash.bin

Đặt FLASH_PORT thành port đang dùng. Kernel config grep phải có bốn dòng =y.
enable-amoled.sh đã chạy linux-dirclean. Không dùng scripts/ci-build.sh.
Nếu output AMOLED chưa tồn tại, tạo coreboard1_defconfig ở output riêng rồi
enable AMOLED như hướng dẫn speed-clean; không áp lại defconfig lên output
đã enable. Nếu checkout fix/co5300-gdma trực tiếp thì không git apply thêm.

## Board: chứng minh DMA active và đo 5 mẫu

Chạy qua UART/SSH, không qua VT trên panel. Không cần Python hoặc pkill.

    dmesg | grep -E 'AXI GDMA|transfer engine|co5300|DMA.*error|timeout'
    cat /sys/class/graphics/fb0/virtual_size
    cat /sys/module/spi_esp32s31/parameters/dma_enable
    cat /sys/module/spi_esp32s31/parameters/pio_poll
    cat /sys/class/spi_master/spi2/device/dma_stats
    cat /sys/bus/spi/devices/*/flush_stats

Kỳ vọng log "AXI GDMA ready" và "transfer engine: AXI GDMA TX SG, max 32768 bytes";
fb0 vẫn 502,410, 40000000 Hz; dma_enable=Y và pio_poll=Y.
dma_stats phải available=Y/enabled=Y, tx_transfers/tx_bytes tăng, errors=0.
Chỉ log "ready" không chứng minh pixels đã đi bằng DMA; counters là kiểm tra
đường TX đã chạy và hoàn tất thành công.

Từ WSL2:

    scp scripts/test-amoled-speed.sh root@192.168.0.182:/tmp/test-amoled-speed.sh

Thay IP theo board. Ở UART/SSH:

    sh /tmp/test-amoled-speed.sh

Script giữ cách test cũ: snapshot RAM framebuffer rồi ghi lại 411640 byte
5 lần, không xoá prompt và không đọc panel qua SPI. Có thêm dma_stats mỗi mẫu.
Mỗi full frame dùng 13 DMA transactions; ngoài cursor update, counters
tăng ít nhất 65 transactions/2058200 bytes qua 5 mẫu. Xem full_* stats,
không dùng total_us của partial/cursor update.

Acceptance cần đo trên board: full_total_us < 100000 cho CẢ 5 mẫu, hướng đến
30000–50000; full_render_us khoảng 15000; errors=0; không timeout; mắt xác
nhận scroll mượt/vạch quét; getty sạch sau reboot. Chưa đánh dấu acceptance
này là đạt.

Fallback/A-B phần mềm:

    printf 'N\n' > /sys/module/spi_esp32s31/parameters/dma_enable
    sh /tmp/test-amoled-speed.sh
    printf 'Y\n' > /sys/module/spi_esp32s31/parameters/dma_enable

N dùng PIO polling đã chứng minh; DMA counters không tăng cho các frame đó.
Parameter có tác dụng khi channels/provider đã probe. Nếu provider không
probe, giữ UART log và dùng flash.bin PIO đã lưu để quay lại bản đã test.
Không yêu cầu kiểm tra lại wiring, reset, SPI reads hay đổi clock.

Gửi lại log probe + 5 mẫu DMA. Nếu bits/transfer nhanh nhưng full_total_us
còn jitter, số liệu đó sẽ tách scheduler/13-completion overhead khỏi transpose.
Không tự gán phép đo PIO cũ cho DMA mới.
