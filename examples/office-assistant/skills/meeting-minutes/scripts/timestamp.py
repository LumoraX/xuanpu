"""按明确时区换算会议日期，DST缺口或未明确的重叠时刻拒绝猜测。"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def main() -> None:
    """返回实际UTC秒和可读时区时间，模型无需心算Unix时间。"""
    local_time,timezone=sys.argv[1:]
    wall=datetime.fromisoformat(local_time)
    zone=ZoneInfo(timezone)
    if wall.tzinfo is not None:
        actual=wall.astimezone(zone)
        if actual.replace(tzinfo=None)!=wall.replace(tzinfo=None) or actual.utcoffset()!=wall.utcoffset():
            raise ValueError('显式偏移与所选时区不一致')
        timestamp=wall.timestamp()
    else:
        matches={wall.replace(tzinfo=zone,fold=fold).timestamp() for fold in (0,1)
            if datetime.fromtimestamp(wall.replace(tzinfo=zone,fold=fold).timestamp(),zone).replace(tzinfo=None)==wall}
        if len(matches)!=1:raise ValueError('当地时刻不存在或有重叠，请明确实际偏移')
        timestamp=next(iter(matches))
    print(json.dumps({'timestamp':timestamp,'timezone':timezone,
        'local_time':datetime.fromtimestamp(timestamp,zone).isoformat(),
        'utc':datetime.fromtimestamp(timestamp,UTC).isoformat()}))


if __name__=='__main__':main()
