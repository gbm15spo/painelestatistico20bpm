#!/usr/bin/env python3
"""Executar localmente. Nunca colocar senha ou hash no repositorio."""
import getpass, hashlib, secrets
p=getpass.getpass('Digite uma SENHA NOVA e forte para o administrador: ')
if len(p)<12:raise SystemExit('Use pelo menos 12 caracteres.')
if p!=getpass.getpass('Confirme a senha: '):raise SystemExit('As senhas nao conferem.')
salt=secrets.token_hex(16);rounds=600_000
h=hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(salt),rounds).hex()
print('Cole como ADMIN_PASSWORD_HASH nas variaveis da Vercel:')
print(f'pbkdf2_sha256${rounds}${salt}${h}')
