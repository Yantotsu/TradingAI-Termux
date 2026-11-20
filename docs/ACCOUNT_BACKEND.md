# Akun undangan dan buku besar simulasi

Status: source backend sudah disiapkan, tetapi belum terhubung ke proyek Supabase produksi dan belum menerima dana nyata.

## Batas cakupan

- Akun harus login dan menukarkan kode undangan acak 192-bit yang terikat ke email penerima terverifikasi, kedaluwarsa maksimal 30 hari, dan hanya dapat dipakai sekali. Kode mentah hanya dikirim saat dibuat; database menyimpan SHA-256-nya.
- Akses data dibatasi dengan PostgreSQL Row Level Security berdasarkan `auth.uid()`. Pengguna hanya dapat membaca profil dan buku besar simulasinya sendiri; perubahan buku besar akun itu memperbarui panelnya secara langsung lewat Supabase Realtime dengan filter user ID dan RLS.
- `demo_ledger` hanya untuk `SIM_IDR`, append-only bagi klien, memiliki kunci idempotensi, serta perubahan demo hanya melalui fungsi admin yang memeriksa admin di database dan dibatasi maksimal ±Rp1 triliun per catatan.
- Deposit uang asli, penarikan, pembayaran, saldo rupiah yang dapat ditagih, dan trading live per pengguna **belum tersedia**. Jangan catat uang orang di `demo_ledger`.

## Rahasia

`SUPABASE_PUBLISHABLE_KEY` boleh dipakai di browser setelah RLS aktif. Jangan pernah menaruh secret/service-role key di HTML, GitHub Pages, atau variabel `PUBLIC_*`. Fungsi ini memakai JWT pengguna dan hak database terbatas; operasi istimewa dikunci dengan tabel `account_admins`. Untuk rahasia produksi gunakan Supabase Dashboard → Edge Function Secrets. Untuk lokal, salin `.env.example` ke `supabase/functions/.env` (sudah diabaikan Git). Jangan commit file `.env`.

## Menyiapkan proyek Supabase

1. Buat proyek Supabase dan catat Project URL serta publishable key. Jangan kirim database password atau secret/service-role key melalui chat.
2. Jalankan migrasi lewat Supabase CLI dari repo privat:

   ```sh
   supabase login
   supabase link --project-ref PROJECT_REF
   supabase db push
   ```
3. Buat akun pemilik melalui Supabase Auth. Dari SQL Editor Supabase, masukkan UUID user pemilik ke daftar admin:

   ```sql
   insert into public.account_profiles(user_id, access_status)
   values ('GANTI_DENGAN_AUTH_USER_UUID', 'active');
   insert into public.account_admins(user_id)
   values ('GANTI_DENGAN_AUTH_USER_UUID');
   ```

4. Deploy fungsi dengan JWT verification aktif:

   ```sh
   supabase functions deploy issue-invite
   supabase functions deploy redeem-invite
   ```

   Jangan menonaktifkan RLS.
5. Pemilik memasukkan email penerima lalu memanggil `issue-invite` saat login; kode tampil sekali, terikat ke email tersebut, dan diberikan langsung kepada orang yang disetujui. UI login dan admin untuk membuat kode tersedia di situs; pemilik harus menyambungkan proyek Supabase lewat variabel GitHub Actions sebelum dipakai.
6. Setelah login, calon pengguna memvalidasi pasangan email/kode sebelum mendaftar, lalu mengirim kode ke `redeem-invite` setelah email terverifikasi. Database mengklaim kode dalam satu transaksi; klaim yang bersamaan tidak dapat memakai kode dua kali.
7. Pastikan Data API hanya mengekspos objek yang perlu; jalankan audit/advisor Supabase dan uji RLS sebelum membuka situs.

## Saldo dan top-up

Admin dapat memberi atau mengoreksi kredit paper melalui panel admin; fungsi `admin_adjust_demo_balance` hanya mengubah saldo simulasi. Ia tidak memverifikasi bukti pembayaran dan **tidak boleh dipakai untuk mengkredit uang asli**. Dana pelanggan/top-up nyata baru dapat dirancang setelah model bisnis dan jalur pembayaran/custody yang sesuai hukum disepakati, termasuk rekonsiliasi, bukti transaksi, penanganan komplain/pengembalian, dan audit.

Jika layanan menerima atau menyimpan nilai pembayaran pengguna, klasifikasi dan izin harus dikonfirmasi dengan penasihat hukum serta mitra pembayaran berizin. Bank Indonesia mengatur perizinan PJP dan menjelaskan penyelenggara dompet elektronik serta payment gateway memerlukan izin. Jika layanan juga mengelola investasi/perdagangan untuk orang lain, kewajiban OJK dapat berlaku bergantung produk dan model operasinya. Ini bukan kesimpulan hukum untuk model bisnis tertentu.
