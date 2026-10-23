-- Miniflux 2.3.4 stores both passwords as bcrypt hashes. Bootstrap Google
-- Reader from the admin login without exposing plaintext credentials.
-- Preserve separately configured integration credentials on subsequent deploys.
UPDATE integrations AS integration
SET googlereader_enabled = true,
    googlereader_username = CASE
        WHEN integration.googlereader_username = '' THEN account.username
        ELSE integration.googlereader_username
    END,
    googlereader_password = CASE
        WHEN integration.googlereader_password = '' THEN account.password
        ELSE integration.googlereader_password
    END
FROM users AS account
WHERE integration.user_id = account.id
  AND account.username = 'admin'
  AND account.is_admin
  AND (NOT integration.googlereader_enabled
       OR integration.googlereader_username = ''
       OR integration.googlereader_password = '');
