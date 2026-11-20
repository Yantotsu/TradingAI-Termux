# Audit bot publik: kondisi dan rancangan operasi

## Hasil audit source saat ini

- `bot.py` adalah satu proses lokal Termux untuk spot Indodax. Pair otomatis hanya membandingkan BTCIDR dan ETHIDR. Belum ada adapter saham atau forex.
- Situs publik menampilkan feed pasar dan sinyal, bukan layanan bot per pengguna. Tidak ada tombol Start/Stop bot yang memulai worker akun pengguna.
- Fondasi Supabase di `supabase/` masih source-only. `auth-config.js` publik masih kosong; login, invite, dan isolasi akun belum pernah dibuktikan di proyek live.
- Tidak ada payment provider, top-up, saldo rupiah nyata, refund, atau rekonsiliasi settlement. Ledger yang ada hanya `SIM_IDR`.
- Model adalah logistic regression kecil dari candle satu jam dengan horizon empat jam. Snapshot model BTCIDR yang diaudit menunjukkan balanced accuracy **49,66%**, di bawah baseline **50,00%**. Hasil ini tidak mendukung trading live.
- Validasi model masih satu potongan kronologis; belum ada laporan walk-forward berulang, net P/L setelah fee dan slippage terukur, latency, drawdown, atau stress test. Satu skor di atas 50% juga tidak cukup untuk menyetujui dana nyata.
- Live adapter lokal dapat menempatkan spot market order dengan API key yang dibuat sendiri oleh pemilik akun. Ia memeriksa agar penarikan IDR/kripto nonaktif, tetapi belum menjadi sistem multi-pengguna dan belum pernah diuji pada akun exchange nyata.

## Perubahan audit lokal

- Candle yang belum selesai atau OHLC tidak konsisten dibuang; celah candle satu jam tidak diperlakukan sebagai candle berurutan untuk pelatihan/prediksi.
- Ekuitas dan batas risiko sekarang menghitung estimasi fee saat melikuidasi posisi.
- Satu instalasi lokal menolak proses bot kedua yang memakai database sama.
- Model hasil pelatihan ditandai belum disetujui untuk live. Satu holdout tidak dapat mengaktifkan model; status persetujuan terpisah harus diberikan setelah evaluasi walk-forward dan biaya penuh lulus.
- Tes unit mencakup sizing, permission withdrawal, deduplikasi fill, lock proses, biaya keluar, candle partial/gap, dan fail-closed model.

## Bentuk layanan multi-pengguna yang akan dipakai

1. Anggota masuk melalui akun terverifikasi dan token undangan sekali pakai.
2. Setiap anggota menghubungkan rekening/perdagangan miliknya sendiri pada broker/exchange yang sesuai dan disetujui untuk integrasi ini. Jangan gunakan satu API key atau satu saldo bersama untuk seluruh anggota.
3. Dana dan aset tetap berada pada rekening pengguna di penyedia tersebut. API key hanya baca dan trading; withdrawal selalu nonaktif. Rahasia disimpan di vault server terenkripsi per pengguna, tidak di browser, repositori, log, atau Termux pengguna lain.
4. Tombol Start hanya membuat sesi worker idempoten untuk satu user/account/pair yang sudah aktif. Tombol Stop mencegah order baru; kill switch operator menghentikan semua worker dan membuat status terlihat.
5. Sebelum order, sistem membaca saldo, posisi, order terbuka, status pair, data segar, model yang disetujui, dan batas risiko. Setiap order punya client ID unik dan dicatat sebelum dikirim.
6. Setelah submit, sistem memeriksa status order dan semua fill dari API. Jika respons ambigu, data tidak cocok, atau koneksi hilang, worker berhenti tanpa mengulang order. Posisi dan transaksi direkonsiliasi dengan data penyedia sebelum lanjut.
7. Saldo, fee, order, posisi, dan hasil ditampilkan per akun dari catatan provider dan ledger audit; tidak dicampur dengan `SIM_IDR`.
8. Pembayaran layanan (jika diperlukan) ditagih sebagai transaksi layanan melalui mitra pembayaran yang sesuai. Top-up ke saldo trading di platform tidak diaktifkan pada rancangan ini. Jika produk akhirnya harus menerima dan menyimpan dana untuk trading pengguna, peluncuran menunggu struktur kustodi/izin/mitra yang disetujui penasihat hukum dan regulator terkait.

## Gerbang model dan rilis

- Uji out-of-sample bergulir beberapa periode tanpa memilih parameter pada periode uji.
- Laporkan baseline, balanced accuracy, kalibrasi, jumlah sampel, net return, fee, spread, slippage, turnover, drawdown maksimum, serta hasil per regime dan per aset.
- Model tetap `HOLD` untuk uang nyata sampai pemilik layanan menyetujui artifact tertentu setelah laporan itu lolos pemeriksaan independen.
- Paper forward test harus berjalan cukup lama untuk meliputi kondisi pasar yang berbeda; hasilnya tidak menjamin hasil live.
- Sandbox broker/payment, akun uji minimal dua pengguna, RLS, restart worker, order duplikat, timeout setelah submit, partial fill, fee aset berbeda, koneksi putus, kill switch, dan pemulihan/reconciliation harus lulus sebelum uji uang riil kecil yang disetujui.
- Tidak ada klaim AI pintar, akurasi tinggi, atau profit terjamin. Sistem boleh tidak membeli ketika bukti model/risk belum cukup.

## Dependensi yang belum tersedia

Proyek Supabase belum dibuat/diisi variabel dan secrets; merchant account sandbox dan webhook secret belum ada; belum ada broker/exchange integration agreement untuk multi-pengguna; belum ada data atau persetujuan legal untuk memegang dana pelanggan. Tanpa hal tersebut, login live, top-up riil, bot publik per anggota, dan settlement tidak bisa diuji nyata.

Rujukan primer: [PBI BI 23/6/PBI/2021 tentang PJP](https://www.bi.go.id/id/publikasi/peraturan/pages/pbi_230621.aspx), [OJK tentang perizinan aset keuangan digital/kripto](https://ojk.go.id/id/fungsi-utama/itsk/perizinan-itsk-aset-keuangan-digital-aset-kripto/default.aspx), dan [panduan keamanan Trade API Indodax](https://help.indodax.com/hc/id/articles/5315217255833-Bagaimana-Cara-Mengintegrasikan-API-INDODAX).
