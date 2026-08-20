from __future__ import annotations

import sqlite3

from engram.db import write_transaction


def build_links(
    connection: sqlite3.Connection,
    record_id: str,
    neighbors: list[tuple[str, float]],
    min_score: float = 0.5,
) -> int:
    """把 kNN 近邻写成双向链接。

    近邻本就是嵌入的副产品，因此建立链接的边际成本接近于零。
    双向写入让任一侧都能直接查到关联记录，无需反向扫描。
    低分近邻（score < min_score）不建边：score 是 1/(1+distance)，
    0.5 对应 distance=1.0，低于它的边在实测中 80% 是噪音。
    """
    pairs = [
        (target, score)
        for target, score in neighbors
        if target != record_id and score >= min_score
    ]
    if not pairs:
        return 0
    written = 0
    with write_transaction(connection) as tx:
        for target, score in pairs:
            for source_id, target_id in ((record_id, target), (target, record_id)):
                tx.execute(
                    """
                    INSERT INTO record_links(
                        source_id, target_id, relation, score, provenance
                    ) VALUES (?, ?, 'related_to', ?, 'knn')
                    ON CONFLICT(source_id, target_id, relation)
                    DO UPDATE SET score = excluded.score
                    """,
                    (source_id, target_id, score),
                )
                written += 1
    return written
