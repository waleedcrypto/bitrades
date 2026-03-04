#!/usr/bin/env python3
"""Bitrade Startup - initializes DB and starts server"""
import sys, os
os.chdir('/home/claude/bitrade')
sys.path.insert(0, '/home/claude/bitrade')

from app import app, init_db

init_db()
print("DB initialized")
app.run(host='0.0.0.0', port=5000, debug=False)
