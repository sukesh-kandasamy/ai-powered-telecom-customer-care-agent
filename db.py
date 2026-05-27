import sqlite3
import os
from datetime import datetime

DB_FILE = "customer_care.db"

def init_db():
    """Initialize the database and create tables if they don't exist."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS support_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            phone_no TEXT NOT NULL,
            issue_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()

def create_support_request(name: str, phone_no: str, issue_type: str) -> int:
    """Create a new support request in the database and return its ID."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO support_requests (name, phone_no, issue_type, created_at)
        VALUES (?, ?, ?, ?)
    ''', (name, phone_no, issue_type, datetime.now()))
    
    request_id = cursor.lastrowid
    conn.commit()
    conn.close()
    
    return request_id

def update_request_status(request_id: int, status: str):
    """Update the status of a specific support request."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    cursor.execute('''
        UPDATE support_requests
        SET status = ?
        WHERE id = ?
    ''', (status, request_id))
    
    conn.commit()
    conn.close()

def get_request_by_id(request_id: int) -> dict:
    """Fetch a request by its ID."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM support_requests WHERE id = ?', (request_id,))
    row = cursor.fetchone()
    conn.close()
    
    if row:
        return dict(row)
    return None

# Initialize the database when the module is imported
init_db()
