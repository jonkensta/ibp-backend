# PostgreSQL Migration Guide

This guide will help you migrate your IBP database from SQLite to PostgreSQL.

## Prerequisites

✅ You have created a Supabase project
✅ You have the PostgreSQL connection string from Supabase

## Step 1: Get Your Supabase Connection String

1. Go to your Supabase project dashboard
2. Navigate to: **Settings** → **Database** → **Connection String**
3. Select **URI** tab
4. Copy the connection string (it looks like):
   ```
   postgresql://postgres.[project-ref]:[YOUR-PASSWORD]@aws-0-us-west-1.pooler.supabase.com:6543/postgres
   ```
5. **Important:** Replace `[YOUR-PASSWORD]` with your actual database password

## Step 2: Set Environment Variable

```bash
# In the server directory
cd /home/jstarr/Source/ibp/server

# Set the DATABASE_URL environment variable
export DATABASE_URL="postgresql://postgres.[project-ref]:your-password@aws-0-us-west-1.pooler.supabase.com:6543/postgres"
```

**Tip:** Save this to a `.env` file for convenience:

```bash
# Create .env file (copy from example)
cp .env.example .env

# Edit .env and paste your Supabase connection string
# Then load it:
source .env  # or: export $(cat .env | xargs)
```

## Step 3: Test PostgreSQL Connection

Before migrating, let's verify the connection works:

```bash
# Activate virtual environment
source .venv/bin/activate

# Test connection with Python
python3 -c "
import asyncio
import os
from ibp.db import get_uri, build_engine

async def test():
    uri = get_uri()
    print(f'Using: {uri.split(\"@\")[0]}@***')
    engine = build_engine()
    async with engine.begin() as conn:
        result = await conn.execute('SELECT 1')
        print('✓ Connection successful!')
    await engine.dispose()

asyncio.run(test())
"
```

If you see `✓ Connection successful!`, you're ready to migrate!

## Step 4: Run Migration Script

```bash
# Make sure DATABASE_URL is set (from Step 2)
echo $DATABASE_URL  # Should print your connection string

# Run the migration
python migrate_to_postgres.py
```

**What the script does:**
1. Creates all tables in PostgreSQL
2. Copies data from `data.db` (SQLite) to PostgreSQL
3. Migrates in correct order (Units → Inmates → Lookups/Requests/Comments)
4. Verifies all data was copied successfully

**Expected output:**
```
============================================================
IBP Database Migration: SQLite → PostgreSQL
============================================================

Source (SQLite): sqlite+aiosqlite:///path/to/data.db
Target (PostgreSQL): postgresql+asyncpg://postgres@***

Creating tables in PostgreSQL...
✓ Tables created

============================================================
Starting data migration...
============================================================

Migrating Units...
  Found 150 Units records
✓ Migrated 150 Units records

Migrating Inmates...
  Found 1250 Inmates records
  Migrated 100/1250...
  Migrated 200/1250...
  ...
✓ Migrated 1250 Inmates records

...

Committing changes...
✓ All changes committed

============================================================
Verifying migration...
============================================================
✓ Units: 150 records (matches SQLite)
✓ Inmates: 1250 records (matches SQLite)
✓ Lookups: 890 records (matches SQLite)
✓ Requests: 2340 records (matches SQLite)
✓ Comments: 456 records (matches SQLite)

============================================================
✓ SUCCESS! Migrated 5086 total records
============================================================
```

## Step 5: Verify Data in Supabase

1. Go to your Supabase dashboard
2. Navigate to: **Table Editor**
3. Check that all tables exist:
   - inmates
   - units
   - lookups
   - requests
   - comments
4. Verify record counts match the migration output

## Step 6: Test Your Application

```bash
# Start the server with PostgreSQL
DATABASE_URL="your-connection-string" uvicorn ibp.api:app --reload

# Test some endpoints
curl http://localhost:8000/units
curl http://localhost:8000/inmates/Texas/12345
```

## Troubleshooting

### Error: "DATABASE_URL environment variable not set"

**Solution:** Set the environment variable:
```bash
export DATABASE_URL="postgresql://..."
```

### Error: "Connection refused" or "Cannot connect to PostgreSQL"

**Possible causes:**
1. Wrong connection string
2. Firewall blocking connection
3. Supabase project paused (free tier pauses after 7 days inactivity)

**Solution:**
- Verify connection string in Supabase dashboard
- Check Supabase project is active (not paused)
- Try connecting from Supabase SQL Editor first

### Error: "Table already exists"

**Cause:** Tables were already created in a previous attempt

**Solution:** Either:
1. Drop all tables in Supabase SQL Editor and re-run migration
2. Or manually delete data and re-run

**SQL to drop all tables:**
```sql
DROP TABLE IF EXISTS comments CASCADE;
DROP TABLE IF EXISTS requests CASCADE;
DROP TABLE IF EXISTS lookups CASCADE;
DROP TABLE IF EXISTS inmates CASCADE;
DROP TABLE IF EXISTS units CASCADE;
```

### Error: Foreign key constraint violation

**Cause:** Tables migrated in wrong order

**Solution:** This shouldn't happen (script migrates in correct order), but if it does:
1. Drop all tables (see above)
2. Re-run migration script

### Verification shows mismatched counts

**Investigation steps:**
1. Check for errors during migration
2. Look at server logs for any issues
3. Manually count records in both databases:

```bash
# SQLite
sqlite3 data.db "SELECT COUNT(*) FROM inmates;"

# PostgreSQL (in Supabase SQL Editor)
SELECT COUNT(*) FROM inmates;
```

## Rollback

If something goes wrong and you need to rollback:

1. **Your SQLite data is safe** - we only READ from `data.db`, never modify it
2. Drop PostgreSQL tables (see SQL above)
3. Fix the issue
4. Re-run migration

## Next Steps

After successful migration:

1. ✅ Update your production environment to use `DATABASE_URL`
2. ✅ Test all API endpoints thoroughly
3. ✅ Keep `data.db` as backup (don't delete it)
4. ✅ Deploy to Google Cloud Run

## Development vs Production

**Development (local):**
- Without `DATABASE_URL`: Uses SQLite (`data.db`)
- With `DATABASE_URL`: Uses PostgreSQL

**Production (Cloud Run):**
- Always uses `DATABASE_URL` environment variable
- Set via `gcloud run deploy --set-env-vars DATABASE_URL=...`

## Support

If you encounter issues:
1. Check error messages carefully
2. Verify Supabase project is active
3. Test connection with simple Python script
4. Check logs in `server.log`
