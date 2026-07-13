import psycopg2
conn = psycopg2.connect(
    host='database-1.cfutwrwyrxts.ap-northeast-1.rds.amazonaws.com',
    port=5432, database='beadsdb', user='harryguo', password='skyla168'
)
cur = conn.cursor()
cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='qbi_qr' AND table_name='paneltype' ORDER BY ordinal_position")
print('columns:', [r[0] for r in cur.fetchall()])
cur.execute("SELECT * FROM qbi_qr.paneltype LIMIT 15")
rows = cur.fetchall()
for r in rows:
    print(r)
cur.close()
conn.close()
