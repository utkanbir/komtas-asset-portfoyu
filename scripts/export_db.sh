#!/usr/bin/env sh
# Çalışan uygulamadaki tüm veriyi seed biçiminde yedekler: ./scripts/export_db.sh https://asset-portfoyu.example > yedek.json
# Kimlik doğrulama gerekiyorsa curl seçeneklerini ekleyin (ör. -u ad:parola veya tarayıcı çerezi).
curl -fsS "${1:-http://localhost:8080}/api/export"
