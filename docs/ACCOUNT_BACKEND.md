# Akun undangan dan buku besar simulasi

Status: source backend sudah disiapkan, tetapi belum terhubung ke proyek Supabase produksi dan belum menerima dana nyata.

## Batas cakupan

- Akun harus login dan menukarkan kode undangan acak 192-bit yang terikat ke email penerima terverifikasi, kedaluwarsa maksimal 30 hari, dan hanya dapat dipakai sekali. Kode mentah hanya dikirim saat dibuat; database menyimpan SHA-256-nya.
- Akses data dibatasi dengan PostgreSQL Row Level Security berdasarkan `auth.uid()`. Pengguna hanya dapat membaca profil dan buku besar simulasinya sendiri; perubahan buku besar akun itu memperbarui panelnya secara langsung lewat Supabase Realtime dengan filter user ID dan RLS.
- `demo_ledger` hanya untuk `SIM_IDR`, append-only bagi klien, memiliki kunci idempotensi, serta perubahan demo hanya melalui fungsi admin yang memeriksa admin di database dan dibatasi maksimal ±Rp1 triliun per catatan.
- Deposit uang asli, penarikan, pembayaran, saldo rupiah yang dapat ditagih, dan trading live per pengguna **belum tersedia**. Jangan catat uang orang di `demo_ledger`.

## Rahasia

`SUPABASE_PUBLISHABLE_KEY` boleh dipakai di browser setelah RLS aktif. Jangan pernah menaruh secret/service-role key di HTML, GitHub Pages, atau variabel `PUBLIC_*`. Fungsi ini memakai JWT pengguna dan hak database terbatas; operasi istimewa dikunci dengan tabel `account_admins`. Untuk rahasia produksi gunakan Supabase Dashboard → Edge Function Secrets. Untuk deployment CLI terotomasi, token Supabase dan password database disimpan sebagai GitHub Actions secrets di repo privat. Untuk lokal, salin `.env.example` ke `supabase/functions/.env` (sudah diabaikan Git). Jangan commit file `.env`.

## Menyiapkan proyek Supabase

1. Buat proyek Supabase dan catat Project URL serta publishable key. Jangan kirim database password atau secret/service-role key melalui chat.
2. Di settings repo privat, tambahkan **Actions variable** `SUPABASE_PROJECT_REF` dan dua **Actions secrets**: `SUPABASE_ACCESS_TOKEN` (token akun Supabase) serta `SUPABASE_DB_PASSWORD`. Jalankan Actions → **Deploy Supabase backend** secara manual. Workflow hanya berjalan dari branch `main`, menerapkan migrasi, dan deploy dua fungsi. Rahasia tidak dicetak ke log. Untuk penguncian tambahan, atur required reviewer pada environment `supabase-production`.
3. Buat akun pemilik melalui Supabase Auth. Dari SQL Editor Supabase, masukkan UUID user pemilik ke daftar admin:

   ```sql
   insert into public.account_profiles(user_id, access_status)
   values ('GANTI_DENGAN_AUTH_USER_UUID', 'active');
   insert into public.account_admins(user_id)
   values ('GANTI_DENGAN_AUTH_USER_UUID');
   ```

4. Workflow backend mendeploy fungsi dengan JWT verification aktif dari `supabase/config.toml`. Jangan menonaktifkan RLS.
5. Pemilik memasukkan email penerima lalu memanggil `issue-invite` saat login; kode tampil sekali, terikat ke email tersebut, dan diberikan langsung kepada orang yang disetujui. UI login dan admin untuk membuat kode tersedia di situs; pemilik harus menyambungkan proyek Supabase lewat variabel GitHub Actions sebelum dipakai.
6. Setelah login, calon pengguna memvalidasi pasangan email/kode sebelum mendaftar, lalu mengirim kode ke `redeem-invite` setelah email terverifikasi. Database mengklaim kode dalam satu transaksi; klaim yang bersamaan tidak dapat memakai kode dua kali.
7. Pastikan Data API hanya mengekspos objek yang perlu; jalankan audit/advisor Supabase dan uji RLS sebelum membuka situs.

## Saldo dan top-up

Admin dapat memberi atau mengoreksi kredit paper melalui panel admin; fungsi `admin_adjust_demo_balance` hanya mengubah saldo simulasi. Ia tidak memverifikasi bukti pembayaran dan **tidak boleh dipakai untuk mengkredit uang asli**. Dana pelanggan/top-up nyata baru dapat dirancang setelah model bisnis dan jalur pembayaran/custody yang sesuai hukum disepakati, termasuk rekonsiliasi, bukti transaksi, penanganan komplain/pengembalian, dan audit.

Jika layanan menerima atau menyimpan nilai pembayaran pengguna, klasifikasi dan izin harus dikonfirmasi dengan penasihat hukum serta mitra pembayaran berizin. Bank Indonesia mengatur perizinan PJP dan menjelaskan penyelenggara dompet elektronik serta payment gateway memerlukan izin. Jika layanan juga mengelola investasi/perdagangan untuk orang lain, kewajiban OJK dapat berlaku bergantung produk dan model operasinya. Ini bukan kesimpulan hukum untuk model bisnis tertentu.


Before connecting a payment provider or enabling real-money operation, complete the [production readiness and payment safety gates](PRODUCTION_READINESS.md). The present schema is intentionally simulation-only.

Admin invitation creation and demo-balance adjustment require Supabase AAL2 (TOTP MFA). The dashboard enrolls an admin in an authenticator app and challenges the factor before showing admin controls; PostgreSQL checks the AAL claim again on every privileged write. Enable TOTP enrollment and verification in the Supabase Auth settings, then test both aal1 rejection and aal2 success before using admin functions.

## Restricted test-monitor account

A test-monitor account is a normal, email-verified account with an account_profiles row and a monitoring_admins row. Do not add it to account_admins. The monitoring role can only read component-level statuses in service_monitoring with an AAL2 session. That table has no user IDs, balances, orders, credentials, or free-form log field; only the trusted service_role can write it. RLS denies it access to other users' profiles and ledgers. The monitor role cannot issue invites or alter any balance.

After creating and verifying the dedicated test account in Supabase Auth, add its UUID using the Supabase SQL Editor:

    insert into public.account_profiles(user_id, access_status)
    values ('TEST_ACCOUNT_AUTH_UUID', 'active');
    insert into public.monitoring_admins(user_id)
    values ('TEST_ACCOUNT_AUTH_UUID');

Do not reuse the owner/admin account for this test role. The monitor must enroll an authenticator factor; the health table is readable only at AAL2. Populate service_monitoring only with coarse subsystem states and an optional build identifier; keep diagnostic detail in protected server logs with separate operator controls.


The restricted monitoring role is distinct from the owner admin role. Its database test suite checks AAL2 enforcement, per-user profile/ledger isolation, and rejection of invite or balance-changing operations. The release workflow should pass the local Supabase RLS tests before this migration is applied to production.

The schema also enforces mutual exclusion between monitoring_admins and account_admins with database triggers. A monitoring account cannot be upgraded to the invitation/balance-admin role by adding it to account_admins; create a different owner account for those duties.
