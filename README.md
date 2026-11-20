# Trading AI Termux — Indodax Spot

Proyek pemantau pasar dan bot paper/live spot untuk Termux dengan model klasifikasi yang dilatih lokal. Folder ini berada di `~/Projects/TradingAI-Termux`, terpisah dari CodexManager.

## Pemilihan pasar dan aturan risiko

- `--symbol AUTO` membandingkan BTCIDR dan ETHIDR saat pertama kali dijalankan, memilih volume transaksi 24 jam terbesar setelah filter spread maksimal 0,5%, lalu menyimpan pilihannya supaya restart mengelola pair yang sama. Untuk memilih ulang, gunakan `--reselect-auto` setelah posisi ditutup. Auto-pick hanya membandingkan dua pair ini.
- Maksimum order default 5% ekuitas terpantau; maksimum satu posisi 5%; batas rugi harian 1%; stop loss 2%; cooldown setelah jual 30 menit. Jika stop 2% tereksekusi dekat harga target, rugi kotor kira-kira 0,1% ekuitas sebelum fee dan gap harga.
- Entry baru dilewati jika spread melebihi 0,5%, transaksi terakhir berumur lebih dari 5 menit, atau nilai order di bawah minimum pair.
- Fee taker dan aturan minimum/kuantitas pair dibaca dari metadata publik Indodax. Simulasi membeli pada ask dan menjual pada bid.

Pelatihan pertama memerlukan internet dan dapat memakan waktu. Gunakan `--training-days 90` untuk mempercepat pelatihan awal, atau `--training-days 730` untuk memakai riwayat lebih panjang. Rentang yang sama dipakai kembali saat membaca cache model. Model dilatih ulang setelah cache berumur 24 jam.

Batas ini adalah preset perangkat lunak, bukan angka optimal dan bukan saran investasi personal. Ekuitas live yang dipakai untuk sizing adalah saldo IDR free+locked ditambah posisi yang dicatat bot; aset lain pada akun tidak dihitung. Batas harian dimulai dari ekuitas saat bot pertama kali melihat tanggal lokal itu. Stop loss dikelola oleh proses bot, bukan order stop di exchange, sehingga tidak bekerja jika Termux mati atau jaringan terputus.

## Mode simulasi

```sh
cd ~/Projects/TradingAI-Termux
python bot.py --mode paper --symbol AUTO --interval 60 --web
```

Sebelum mulai memantau, bot mengambil candle 1 jam Indodax selama 365 hari, melatih logistic regression lokal, lalu menyimpan model di `data/models/`. Model dilatih saat bot mulai; cache berumur kurang dari 24 jam akan dipakai kembali. Jika ingin melatih ulang, hentikan lalu jalankan lagi setelah cache berumur 24 jam. Siklus monitor tetap memakai interval yang dipilih, sementara fitur prediksi bersumber dari candle 1 jam terbaru. Dashboard terminal berbentuk tabel metrik. Situs publik GitHub Pages ada di repo terpisah [`Yantotsu/TradingAI-Termux-Site`](https://github.com/Yantotsu/TradingAI-Termux-Site) dan tersedia di https://yantotsu.github.io/TradingAI-Termux-Site/. Workflow memperbarui grafik gunung BTCIDR dan sinyal pasar kira-kira setiap 15 menit. Saldo, posisi, dan order akun tidak dipublikasikan. Kode bot tetap di repo privat ini.

Untuk dashboard lokal yang membaca status bot secara langsung (termasuk saldo/posisi simulasi), jalankan dengan `--web` lalu buka `http://127.0.0.1:8765` di ponsel yang sama. Dashboard lokal aktif selama bot berjalan; animasi BUY/SELL/STOP tampil saat order terjadi. Data saldo simulasi dan transaksi tersimpan di `data/trading.sqlite3`. Hentikan dengan `Ctrl+C`.

Contoh konfigurasi paper:

```sh
python bot.py --mode paper --symbol AUTO --interval 60 \
  --initial-idr 1000000 --max-order-pct 5 --max-position-pct 5 \
  --daily-loss-pct 1 --stop-loss-pct 2 --max-spread-pct 0.5 \
  --training-days 365
```

Saldo awal bersifat virtual dan hanya diterapkan saat akun pair pertama kali dibuat. Model adalah logistic regression yang dilatih ulang dari OHLC historis, dengan fitur return 1/3/6/12 jam, volatilitas, bentuk candle, dan volume. Targetnya adalah kenaikan empat jam ke depan yang melampaui estimasi fee taker dua sisi plus buffer slippage 0,1%. Data validasi memakai bagian waktu paling akhir secara kronologis dan empat candle dipurge agar label tidak bocor melewati batas validasi. Mode paper memakai ambang eksperimen BUY ≥ 20% dan SELL ≤ 15% agar simulasi dapat mengumpulkan contoh transaksi meski validasi lemah. Dashboard menandainya sebagai eksperimen dan sinyalnya tidak layak dipakai live. Mode live tetap mensyaratkan validasi berimbang > 50%, BUY ≥ 60%, dan SELL ≤ 40%. BUY pada kedua mode masih melewati filter spread, cooldown, saldo, minimum order, dan batas risiko. SELL hanya menutup posisi bot yang tercatat; stop-loss dan batas rugi harian tetap menjadi pemicu keluar.

Ini model ML sederhana yang dilatih pada data pasar, bukan model AI besar atau jaminan prediksi benar. Nilai validasi historis bukan jaminan hasil berikutnya. Simulasi belum setara backtest: slippage selain buffer label, latency, spread historis, dan dampak market order belum dimodelkan lengkap.

## Mode live

Live hanya berjalan jika dipilih eksplisit, tersedia `INDODAX_API_KEY` dan `INDODAX_API_SECRET` di environment lokal, API key lolos pemeriksaan izin, dan perintah memuat `--live-confirm I_ACCEPT_REAL_TRADING_RISK`. Key harus memiliki izin baca dan spot trading serta tanpa izin penarikan crypto maupun IDR. Jangan pernah kirim API key/secret melalui chat atau taruh di source code.

API adapter hanya menyediakan pembacaan akun/order/trade, pembuatan spot market order, dan pemeriksaan order terbuka; tidak ada fungsi withdrawal. Jika order terkirim tetapi fill tidak dapat dipastikan, bot menyimpan status pending dan berhenti untuk menghindari order ganda. Periksa order/trade di Indodax dan rekonsiliasi sebelum menjalankan lagi. Bot juga berhenti bila ada order pair terbuka atau saldo posisi tidak cocok.

Contoh perintah live (hanya setelah environment key sudah diatur sendiri di Termux):

```sh
python bot.py --mode live --symbol AUTO --interval 60 \
  --max-order-pct 5 --max-position-pct 5 --daily-loss-pct 1 \
  --stop-loss-pct 2 --max-spread-pct 0.5 \
  --live-confirm I_ACCEPT_REAL_TRADING_RISK
```

Live belum pernah dijalankan atau diverifikasi dengan akunmu. Mulai dari paper terlebih dahulu. Android dapat menghentikan proses latar belakang; koneksi dan Termux harus tetap aktif agar pemantauan berjalan. Saya bisa membantu merawat kode saat kamu kembali, tetapi tidak bisa mengawasi bot terus-menerus dari luar sesi ini.
