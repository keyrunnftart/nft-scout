import sys

if len(sys.argv) > 1 and sys.argv[1] == "scan":   # dev: python -m nft_scout scan
    import json

    from .scan import live_drops, run_scan

    s = run_scan()
    print(json.dumps({"top5": s["top5"], "top5_art": s["top5_art"], "live": live_drops(s)}, indent=1))
elif len(sys.argv) > 1 and sys.argv[1] == "watch":   # unattended tick (Task Scheduler / cron)
    import json

    from .scan import watch

    r = watch()
    print(json.dumps({"rescanned": r["rescanned"], "pings": sum(x.get("sent", False) for x in r["pings"]),
                      "wallet_pings": sum(x.get("sent", False) for x in r["wallet"])}))
else:
    from . import main

    main()
