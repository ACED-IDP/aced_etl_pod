
# to drop a database

```commandline
psql -U postgres -W
DROP DATABASE sheepdog_local with (force);
```

Then redeploy helm chart.
