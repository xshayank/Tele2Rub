#!/usr/bin/env python3
"""
Database Migration Script: SQLite to PostgreSQL
Migrates all tables and data from iran.db (SQLite) to PostgreSQL
"""

import sqlite3
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
import sys


# PostgreSQL connection credentials
PG_CONFIG = {
    'host': '127.0.0.1',
    'port': 5781,
    'user': 'rubedb',
    'password': 'AOEOFVNMASOEGJ4',
    'database': 'rubedb'
}

SQLITE_DB = 'iran.db'


def get_sqlite_connection():
    """Connect to SQLite database"""
    try:
        conn = sqlite3.connect(SQLITE_DB)
        conn.row_factory = sqlite3.Row
        print(f"✓ Connected to SQLite database: {SQLITE_DB}")
        return conn
    except sqlite3.Error as e:
        print(f"✗ Error connecting to SQLite: {e}")
        sys.exit(1)


def get_postgres_connection():
    """Connect to PostgreSQL database"""
    try:
        conn = psycopg2.connect(**PG_CONFIG)
        print(f"✓ Connected to PostgreSQL database: {PG_CONFIG['database']}")
        return conn
    except psycopg2.Error as e:
        print(f"✗ Error connecting to PostgreSQL: {e}")
        sys.exit(1)


def get_sqlite_tables(sqlite_conn):
    """Get all table names from SQLite database"""
    cursor = sqlite_conn.cursor()
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY name
    """)
    tables = [row[0] for row in cursor.fetchall()]
    print(f"✓ Found {len(tables)} tables in SQLite: {', '.join(tables)}")
    return tables


def map_sqlite_type_to_postgres(sqlite_type):
    """Map SQLite data types to PostgreSQL data types"""
    sqlite_type = sqlite_type.upper()
    
    type_mapping = {
        'INTEGER': 'INTEGER',
        'INT': 'INTEGER',
        'TINYINT': 'SMALLINT',
        'SMALLINT': 'SMALLINT',
        'MEDIUMINT': 'INTEGER',
        'BIGINT': 'BIGINT',
        'UNSIGNED BIG INT': 'BIGINT',
        'INT2': 'SMALLINT',
        'INT8': 'BIGINT',
        'REAL': 'REAL',
        'DOUBLE': 'DOUBLE PRECISION',
        'DOUBLE PRECISION': 'DOUBLE PRECISION',
        'FLOAT': 'DOUBLE PRECISION',
        'NUMERIC': 'NUMERIC',
        'DECIMAL': 'DECIMAL',
        'BOOLEAN': 'BOOLEAN',
        'DATE': 'DATE',
        'DATETIME': 'TIMESTAMP',
        'TIMESTAMP': 'TIMESTAMP',
        'TEXT': 'TEXT',
        'CLOB': 'TEXT',
        'BLOB': 'BYTEA',
        'VARCHAR': 'VARCHAR',
        'CHARACTER': 'CHAR',
        'VARYING CHARACTER': 'VARCHAR',
        'NCHAR': 'CHAR',
        'NATIVE CHARACTER': 'VARCHAR',
        'NVARCHAR': 'VARCHAR',
    }
    
    # Check for VARCHAR with size
    if 'VARCHAR' in sqlite_type or 'CHAR' in sqlite_type:
        return sqlite_type.replace('CHARACTER', 'CHAR')
    
    # Default mapping
    for key, value in type_mapping.items():
        if key in sqlite_type:
            return value
    
    # Default to TEXT if type not recognized
    return 'TEXT'


def get_table_schema(sqlite_conn, table_name):
    """Get table schema from SQLite"""
    cursor = sqlite_conn.cursor()
    cursor.execute(f"PRAGMA table_info({table_name})")
    columns = cursor.fetchall()
    
    schema = []
    for col in columns:
        col_name = col[1]
        col_type = map_sqlite_type_to_postgres(col[2])
        not_null = 'NOT NULL' if col[3] else ''
        primary_key = 'PRIMARY KEY' if col[5] else ''
        
        schema.append(f'"{col_name}" {col_type} {not_null} {primary_key}'.strip())
    
    return schema


def create_postgres_table(pg_conn, table_name, schema):
    """Create table in PostgreSQL"""
    cursor = pg_conn.cursor()
    
    # Drop table if exists
    drop_query = sql.SQL("DROP TABLE IF EXISTS {} CASCADE").format(
        sql.Identifier(table_name)
    )
    cursor.execute(drop_query)
    
    # Create table
    create_query = sql.SQL("CREATE TABLE {} ({})").format(
        sql.Identifier(table_name),
        sql.SQL(', '.join(schema))
    )
    
    try:
        cursor.execute(create_query)
        pg_conn.commit()
        print(f"  ✓ Created table: {table_name}")
    except psycopg2.Error as e:
        print(f"  ✗ Error creating table {table_name}: {e}")
        pg_conn.rollback()
        raise


def copy_table_data(sqlite_conn, pg_conn, table_name):
    """Copy all data from SQLite table to PostgreSQL table"""
    sqlite_cursor = sqlite_conn.cursor()
    pg_cursor = pg_conn.cursor()
    
    # Get all data from SQLite
    sqlite_cursor.execute(f"SELECT * FROM {table_name}")
    rows = sqlite_cursor.fetchall()
    
    if not rows:
        print(f"  ✓ No data to copy for table: {table_name}")
        return 0
    
    # Get column names
    column_names = [description[0] for description in sqlite_cursor.description]
    
    # Prepare INSERT query
    insert_query = sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table_name),
        sql.SQL(', ').join(map(sql.Identifier, column_names)),
        sql.SQL(', ').join(sql.Placeholder() * len(column_names))
    )
    
    # Insert data in batches
    batch_size = 1000
    total_rows = len(rows)
    
    try:
        for i in range(0, total_rows, batch_size):
            batch = rows[i:i + batch_size]
            pg_cursor.executemany(insert_query, batch)
            pg_conn.commit()
            print(f"  → Copied {min(i + batch_size, total_rows)}/{total_rows} rows", end='\r')
        
        print(f"\n  ✓ Copied {total_rows} rows to table: {table_name}")
        return total_rows
    
    except psycopg2.Error as e:
        print(f"\n  ✗ Error copying data to {table_name}: {e}")
        pg_conn.rollback()
        raise


def migrate_database():
    """Main migration function"""
    print("\n" + "="*60)
    print("SQLite to PostgreSQL Migration")
    print("="*60 + "\n")
    
    # Connect to databases
    sqlite_conn = get_sqlite_connection()
    pg_conn = get_postgres_connection()
    
    try:
        # Get all tables from SQLite
        tables = get_sqlite_tables(sqlite_conn)
        
        if not tables:
            print("\n✗ No tables found in SQLite database")
            return
        
        total_rows_copied = 0
        
        # Migrate each table
        print(f"\n{'='*60}")
        print("Starting migration...")
        print("="*60 + "\n")
        
        for i, table_name in enumerate(tables, 1):
            print(f"[{i}/{len(tables)}] Migrating table: {table_name}")
            
            # Get schema
            schema = get_table_schema(sqlite_conn, table_name)
            
            # Create table in PostgreSQL
            create_postgres_table(pg_conn, table_name, schema)
            
            # Copy data
            rows_copied = copy_table_data(sqlite_conn, pg_conn, table_name)
            total_rows_copied += rows_copied
            
            print()
        
        print("="*60)
        print(f"✓ Migration completed successfully!")
        print(f"✓ Total tables migrated: {len(tables)}")
        print(f"✓ Total rows copied: {total_rows_copied}")
        print("="*60 + "\n")
    
    except Exception as e:
        print(f"\n✗ Migration failed: {e}")
        sys.exit(1)
    
    finally:
        sqlite_conn.close()
        pg_conn.close()
        print("✓ Database connections closed")


if __name__ == "__main__":
    migrate_database()
