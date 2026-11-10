"""命中即停止的历史查询不能先把整张历史结果取到Python内存。"""

from typing import cast
from sqlite3 import Connection
import json

from app import database as db
from app.modules import recognition_knowledge as knowledge
from app.repositories import local_media
from tests.support import IsolatedDatabaseTestCase, isolated_test_database


class ObservedCursor:
    def __init__(self, cursor):
        self.cursor, self.fetched, self.closed = cursor, 0, False

    def __iter__(self):
        for row in self.cursor:
            self.fetched += 1
            yield row

    def fetchall(self):
        rows = self.cursor.fetchall()
        self.fetched += len(rows)
        return rows

    def close(self):
        self.closed = True
        self.cursor.close()


class ObservedConnection:
    def __init__(self, conn):
        self.conn = conn
        self.cursors = []

    def execute(self, sql, parameters=()):
        cursor = ObservedCursor(self.conn.execute(sql, parameters))
        self.cursors.append(cursor)
        return cursor


class StreamingHistoryQueryTests(IsolatedDatabaseTestCase):
    def setUp(self):
        self.enterContext(isolated_test_database())
        self.source_id = local_media.create_local_media_source(
            name='streaming', qb_profile='', qb_path_prefix='', local_root='/synthetic/stream', owner='admin',
        )
        with db.get_conn() as conn:
            conn.execute('DELETE FROM recognition_knowledge')
            conn.executemany(
                "INSERT INTO recognition_knowledge(knowledge_key,knowledge_type,canonical_value,normalized_value,aliases_json,source,confidence,disabled,user_modified,evidence_json,created_at,updated_at) VALUES(?,'release_group',?,?,?,'user',1,0,1,'{}',?,?)",
                [(f'k{i}', f'G{i}', f'g{i}', json.dumps([f'G{i}']), db.now(), db.now()) for i in range(2000)],
            )
            conn.executemany(
                "INSERT INTO local_media_tasks(source_id,owner,content_path,status,operation_token,created_at,updated_at) VALUES(?,'admin',?,'failed',?,?,?)",
                [(self.source_id, f'/synthetic/stream/f-{i}.mkv', f'token-{i}', db.now(), db.now()) for i in range(2000)],
            )

    def test_knowledge_first_match_reads_one_row_and_closes_cursor(self):
        with db.get_conn() as conn:
            observed = ObservedConnection(conn)
            row = knowledge._find_by_normalized(observed, 'release_group', {'g0'})
            assert row is not None
            self.assertEqual(row['canonical_value'], 'G0')
            self.assertEqual(observed.cursors[0].fetched, 1)
            self.assertTrue(observed.cursors[0].closed)

    def test_knowledge_exclusion_and_not_found_preserve_complete_scan_semantics(self):
        with db.get_conn() as conn:
            first = conn.execute('SELECT id FROM recognition_knowledge ORDER BY id LIMIT 1').fetchone()['id']
            observed = ObservedConnection(conn)
            row = knowledge._find_by_normalized(observed, 'release_group', {'g0', 'g1'}, exclude_id=first)
            assert row is not None
            self.assertEqual(row['canonical_value'], 'G1')
            self.assertEqual(observed.cursors[0].fetched, 2)
            self.assertIsNone(knowledge._find_by_normalized(observed, 'release_group', {'missing'}))
            self.assertEqual(observed.cursors[-1].fetched, 2000)
            self.assertTrue(all(cursor.closed for cursor in observed.cursors))

    def test_latest_task_match_reads_one_row_and_preserves_normalized_history(self):
        with db.get_conn() as conn:
            conn.execute("UPDATE local_media_tasks SET content_path='/synthetic//stream/f-1999.mkv' WHERE id=(SELECT MAX(id) FROM local_media_tasks)")
            observed = ObservedConnection(conn)
            row = local_media._latest_terminal_local_media_task_for_path(
                cast(Connection, observed), source_id=self.source_id, owner='admin', content_path='/synthetic/stream/f-1999.mkv',
            )
            assert row is not None
            self.assertEqual(row['operation_token'], 'token-1999')
            self.assertEqual(observed.cursors[0].fetched, 1)
            self.assertTrue(observed.cursors[0].closed)

    def test_latest_task_oldest_and_absent_paths_keep_full_history_semantics(self):
        with db.get_conn() as conn:
            for path, expected in [('/synthetic/stream/f-0.mkv', 'token-0'), ('/synthetic/stream/missing.mkv', None)]:
                with self.subTest(path=path):
                    observed = ObservedConnection(conn)
                    row = local_media._latest_terminal_local_media_task_for_path(
                        cast(Connection, observed), source_id=self.source_id, owner='admin', content_path=path,
                    )
                    self.assertEqual(row['operation_token'] if row else None, expected)
                    self.assertEqual(observed.cursors[0].fetched, 2000)
                    self.assertTrue(observed.cursors[0].closed)
