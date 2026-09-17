@echo off

cd backend

set IMPORT_ALLOWED_ROOTS=D:\iq_broadcast_hackr\Import

uvicorn main:app --reload --host 127.0.0.1 --port 8000