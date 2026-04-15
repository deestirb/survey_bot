# database.py
import sqlite3
import json
from questions import QUESTIONS

DB_FILE = "survey_results.db"


def get_column_names():
    """Build the list of column names from the questions list."""
    cols = []
    for i in range(len(QUESTIONS)):
        cols.append(f"q{i+1}_answer")
        cols.append(f"q{i+1}_seconds")  # time spent on this question
    return cols


def init_db():
    """Create the database and table if they don't exist yet."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Build column definitions dynamically from your questions list
    dynamic_cols = []
    for i in range(len(QUESTIONS)):
        dynamic_cols.append(f"q{i+1}_answer TEXT")
        dynamic_cols.append(f"q{i+1}_seconds REAL")

    dynamic_cols_sql = ",\n    ".join(dynamic_cols)

    cursor.execute(f"""
        CREATE TABLE IF NOT EXISTS responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            first_name TEXT,
            start_time TEXT,
            end_time TEXT,
            total_seconds REAL,
            completed INTEGER DEFAULT 0,
            {dynamic_cols_sql}
        )
    """)
    conn.commit()
    conn.close()
    print("Database ready.")


def create_response_row(user_id, username, first_name, start_time):
    """
    Insert a new empty row when the user starts the survey.
    Returns the row ID so we can update it as answers come in.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO responses (user_id, username, first_name, start_time)
        VALUES (?, ?, ?, ?)
    """, (user_id, username, first_name, start_time))
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return row_id


def save_answer(row_id, question_index, answer, seconds_spent):
    """
    Update a specific question's answer and time spent for an existing row.
    question_index is 0-based (so question 1 = index 0).
    """
    q_num = question_index + 1
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(f"""
        UPDATE responses
        SET q{q_num}_answer = ?, q{q_num}_seconds = ?
        WHERE id = ?
    """, (answer, round(seconds_spent, 2), row_id))
    conn.commit()
    conn.close()


def finalize_response(row_id, end_time, total_seconds):
    """Mark the survey as complete and record the end time."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE responses
        SET end_time = ?, total_seconds = ?, completed = 1
        WHERE id = ?
    """, (end_time, round(total_seconds, 2), row_id))
    conn.commit()
    conn.close()


def get_stats():
    """Return basic stats for the /stats admin command."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM responses WHERE completed = 1")
    completed = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM responses WHERE completed = 0")
    in_progress = cursor.fetchone()[0]
    cursor.execute("SELECT AVG(total_seconds) FROM responses WHERE completed = 1")
    avg_time = cursor.fetchone()[0]
    conn.close()
    return completed, in_progress, avg_time
