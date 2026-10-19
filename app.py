from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response
import sqlite3, os, csv, io, json
from datetime import date, datetime, timedelta
from functools import wraps

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, 'nutrisafe.db')
app = Flask(__name__)
app.secret_key = os.environ.get('NUTRISAFE_SECRET', 'change-this-secret-before-deployment')

ALLERGENS = ['peanut', 'egg', 'milk', 'wheat']

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    return conn

def init_db():
    c = db()
    c.executescript('''
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL, role TEXT NOT NULL, display_name TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS pupils(id INTEGER PRIMARY KEY, student_id TEXT UNIQUE NOT NULL, first_name TEXT NOT NULL, last_name TEXT NOT NULL, grade TEXT, guardian TEXT, allergy_notes TEXT DEFAULT '', active INTEGER DEFAULT 1, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS allergies(id INTEGER PRIMARY KEY, pupil_id INTEGER NOT NULL REFERENCES pupils(id) ON DELETE CASCADE, allergen TEXT NOT NULL, severity TEXT DEFAULT 'Not specified', verified INTEGER DEFAULT 0, notes TEXT DEFAULT '', UNIQUE(pupil_id, allergen));
    CREATE TABLE IF NOT EXISTS attendance(id INTEGER PRIMARY KEY, pupil_id INTEGER NOT NULL REFERENCES pupils(id) ON DELETE CASCADE, day TEXT NOT NULL, status TEXT NOT NULL, meal_count INTEGER DEFAULT 0, UNIQUE(pupil_id,day));
    CREATE TABLE IF NOT EXISTS menus(id INTEGER PRIMARY KEY, day TEXT NOT NULL, dish TEXT NOT NULL, ingredients TEXT NOT NULL, notes TEXT DEFAULT '', approved INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS inventory(id INTEGER PRIMARY KEY, item TEXT UNIQUE NOT NULL, quantity REAL NOT NULL DEFAULT 0, unit TEXT NOT NULL DEFAULT 'kg', updated_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS forecasts(id INTEGER PRIMARY KEY, day TEXT NOT NULL, rice_kg REAL NOT NULL, viand_portions INTEGER NOT NULL, method TEXT NOT NULL, notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, username TEXT, action TEXT NOT NULL, detail TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    ''')
    # Demo accounts are for local evaluation only; passwords must be changed before real deployment.
    for username, password, role, name in [('admin','NutriSafe123!','admin','System Administrator'),('coordinator','NutriSafe123!','coordinator','Feeding Coordinator'),('nurse','NutriSafe123!','nurse','School Nurse'),('teacher','NutriSafe123!','teacher','Teacher')]:
        c.execute('INSERT OR IGNORE INTO users(username,password,role,display_name) VALUES(?,?,?,?)',(username,password,role,name))
    if c.execute('SELECT COUNT(*) n FROM pupils').fetchone()['n'] == 0:
        demo=[('BCES-001','Ana','Santos','Grade 1','Sample Guardian'),('BCES-002','Miguel','Reyes','Grade 2','Sample Guardian'),('BCES-003','Lia','Dela Cruz','Grade 3','Sample Guardian'),('BCES-004','Noah','Garcia','Grade 4','Sample Guardian'),('BCES-005','Mia','Ramos','Grade 5','Sample Guardian')]
        for sid,fn,ln,gr,gu in demo: c.execute('INSERT INTO pupils(student_id,first_name,last_name,grade,guardian) VALUES(?,?,?,?,?)',(sid,fn,ln,gr,gu))
        ids=[r['id'] for r in c.execute('SELECT id FROM pupils ORDER BY id').fetchall()]
        c.execute('INSERT OR IGNORE INTO allergies(pupil_id,allergen,severity,verified,notes) VALUES(?,?,?,?,?)',(ids[0],'egg','Moderate',1,'SAMPLE DATA — confirm with guardian/health professional'))
        c.execute('INSERT OR IGNORE INTO allergies(pupil_id,allergen,severity,verified,notes) VALUES(?,?,?,?,?)',(ids[1],'milk','Not specified',0,'SAMPLE DATA — requires verification'))
        for n in range(1,8):
            d=(date.today()-timedelta(days=n)).isoformat()
            for i,pid in enumerate(ids):
                c.execute('INSERT OR IGNORE INTO attendance(pupil_id,day,status,meal_count) VALUES(?,?,?,?)',(pid,d,'Present' if (i+n)%5 else 'Absent',0 if (i+n)%5 else 1))
    for item,qty,unit in [('Rice',40,'kg'),('Chicken',25,'kg'),('Vegetables',18,'kg')]:
        c.execute('INSERT OR IGNORE INTO inventory(item,quantity,unit) VALUES(?,?,?)',(item,qty,unit))
    c.commit(); c.close()

init_db()

def log(action, detail=''):
    c=db(); c.execute('INSERT INTO audit(username,action,detail) VALUES(?,?,?)',(session.get('username','system'),action,detail)); c.commit(); c.close()

def login_required(f):
    @wraps(f)
    def wrapped(*a,**kw):
        if not session.get('user_id'): return redirect(url_for('login'))
        return f(*a,**kw)
    return wrapped

def roles(*allowed):
    def deco(f):
        @wraps(f)
        def wrapped(*a,**kw):
            if not session.get('user_id'): return redirect(url_for('login'))
            if session.get('role') not in allowed:
                flash('Your account does not have permission to perform that action.','error'); return redirect(url_for('dashboard'))
            return f(*a,**kw)
        return wrapped
    return deco

@app.context_processor
def common(): return {'today':date.today().isoformat(),'role':session.get('role'),'user_name':session.get('display_name'),'allergens':ALLERGENS}

@app.route('/')
def home(): return redirect(url_for('dashboard') if session.get('user_id') else url_for('login'))

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method=='POST':
        c=db(); u=c.execute('SELECT * FROM users WHERE username=? AND password=?',(request.form.get('username','').strip(),request.form.get('password',''))).fetchone(); c.close()
        if u:
            session.update(user_id=u['id'],username=u['username'],role=u['role'],display_name=u['display_name']); log('login','Successful sign-in'); return redirect(url_for('dashboard'))
        flash('Invalid username or password.','error')
    return render_template('login.html')

@app.route('/logout')
def logout(): log('logout','Signed out'); session.clear(); return redirect(url_for('login'))

@app.route('/dashboard')
@login_required
def dashboard():
    c=db(); total=c.execute('SELECT COUNT(*) n FROM pupils WHERE active=1').fetchone()['n']; today_present=c.execute("SELECT COUNT(*) n FROM attendance WHERE day=? AND status='Present'",(date.today().isoformat(),)).fetchone()['n']; alerts=screen_conflicts(c); inv=c.execute('SELECT * FROM inventory ORDER BY item').fetchall(); recent=c.execute('SELECT * FROM forecasts ORDER BY day DESC,id DESC LIMIT 1').fetchone(); c.close()
    return render_template('dashboard.html',total=total,present=today_present,alerts=alerts,inventory=inv,forecast=recent)

@app.route('/pupils',methods=['GET','POST'])
@login_required
def pupils():
    c=db()
    if request.method=='POST' and session.get('role') in ('admin','coordinator','nurse'):
        sid=request.form.get('student_id','').strip(); fn=request.form.get('first_name','').strip(); ln=request.form.get('last_name','').strip()
        if not sid or not fn or not ln: flash('Student ID, first name, and last name are required.','error')
        else:
            try: c.execute('INSERT INTO pupils(student_id,first_name,last_name,grade,guardian) VALUES(?,?,?,?,?)',(sid,fn,ln,request.form.get('grade',''),request.form.get('guardian',''))); c.commit(); log('create pupil',sid); flash('Pupil record added.','success')
            except sqlite3.IntegrityError: flash('That Student ID already exists.','error')
        c.close(); return redirect(url_for('pupils'))
    q=request.args.get('q','').strip(); rows=c.execute("SELECT p.*, (SELECT group_concat(a.allergen, ', ') FROM allergies a WHERE a.pupil_id=p.id) AS allergy_list FROM pupils p WHERE p.active=1 AND (p.student_id LIKE ? OR p.first_name LIKE ? OR p.last_name LIKE ?) ORDER BY p.last_name",(f'%{q}%',f'%{q}%',f'%{q}%')).fetchall(); c.close()
    return render_template('pupils.html',pupils=rows,q=q)

@app.route('/pupils/<int:pid>/archive',methods=['POST'])
@roles('admin','coordinator')
def archive_pupil(pid):
    c=db(); c.execute('UPDATE pupils SET active=0 WHERE id=?',(pid,)); c.commit(); c.close(); log('archive pupil',str(pid)); flash('Pupil archived. Records are retained for reporting.','success'); return redirect(url_for('pupils'))

@app.route('/pupils/<int:pid>/allergies',methods=['GET','POST'])
@roles('admin','coordinator','nurse')
def pupil_allergies(pid):
    c=db(); pupil=c.execute('SELECT * FROM pupils WHERE id=?',(pid,)).fetchone()
    if not pupil: c.close(); return 'Pupil not found',404
    if request.method=='POST':
        allergen=request.form.get('allergen','').lower()
        if allergen not in ALLERGENS: flash('Choose an allergen from the configured list.','error')
        else:
            c.execute('INSERT INTO allergies(pupil_id,allergen,severity,verified,notes) VALUES(?,?,?,?,?) ON CONFLICT(pupil_id,allergen) DO UPDATE SET severity=excluded.severity,verified=excluded.verified,notes=excluded.notes',(pid,allergen,request.form.get('severity','Not specified'),1 if request.form.get('verified') else 0,request.form.get('notes',''))); c.commit(); log('update allergy',f'pupil={pid}, allergen={allergen}'); flash('Allergy record saved. Verify all health information with an authorized professional.','success')
        c.close(); return redirect(url_for('pupil_allergies',pid=pid))
    allergies=c.execute('SELECT * FROM allergies WHERE pupil_id=? ORDER BY allergen',(pid,)).fetchall(); c.close(); return render_template('allergies.html',pupil=pupil,records=allergies)

@app.route('/attendance',methods=['GET','POST'])
@roles('admin','coordinator','nurse','teacher')
def attendance():
    day=request.values.get('day',date.today().isoformat()); c=db()
    if request.method=='POST':
        for key,val in request.form.items():
            if key.startswith('status_'):
                pid=int(key.split('_')[1]); status=val
                if status not in ('Present','Absent','Excused'): continue
                c.execute('INSERT INTO attendance(pupil_id,day,status,meal_count) VALUES(?,?,?,?) ON CONFLICT(pupil_id,day) DO UPDATE SET status=excluded.status,meal_count=excluded.meal_count',(pid,day,status,1 if status=='Present' else 0))
        c.commit(); log('save attendance',day); flash('Attendance saved.','success'); c.close(); return redirect(url_for('attendance',day=day))
    pupils=c.execute('SELECT * FROM pupils WHERE active=1 ORDER BY last_name').fetchall(); existing={r['pupil_id']:r['status'] for r in c.execute('SELECT pupil_id,status FROM attendance WHERE day=?',(day,)).fetchall()}; c.close(); return render_template('attendance.html',pupils=pupils,day=day,existing=existing)

@app.route('/menus',methods=['GET','POST'])
@roles('admin','coordinator','nurse')
def menus():
    c=db()
    if request.method=='POST':
        dish=request.form.get('dish','').strip(); ingredients=request.form.get('ingredients','').strip(); day=request.form.get('day',date.today().isoformat())
        if not dish or not ingredients: flash('Dish and ingredients are required.','error')
        else:
            c.execute('INSERT INTO menus(day,dish,ingredients,notes) VALUES(?,?,?,?)',(day,dish,ingredients,request.form.get('notes',''))); c.commit(); log('create menu',dish); flash('Menu saved. Run allergy screening before approval.','success')
        c.close(); return redirect(url_for('menus'))
    rows=c.execute('SELECT * FROM menus ORDER BY day DESC,id DESC').fetchall(); c.close(); return render_template('menus.html',menus=rows)

def screen_conflicts(c):
    result=[]
    menus=c.execute('SELECT * FROM menus WHERE day>=? ORDER BY day,id',(date.today().isoformat(),)).fetchall()
    for m in menus:
        tokens=set(x.strip().lower() for x in m['ingredients'].replace(';',',').split(','))
        for a in c.execute('SELECT a.*,p.student_id,p.first_name,p.last_name FROM allergies a JOIN pupils p ON p.id=a.pupil_id WHERE p.active=1').fetchall():
            # conservative keyword match: exact ingredient token or ingredient text containing allergen
            if any(a['allergen'] in token for token in tokens): result.append({'day':m['day'],'dish':m['dish'],'student_id':a['student_id'],'pupil':a['first_name']+' '+a['last_name'],'allergen':a['allergen'],'verified':a['verified'],'ingredient':next((t for t in tokens if a['allergen'] in t),a['allergen'])})
    return result

@app.route('/allergy-screening')
@roles('admin','coordinator','nurse')
def screening():
    c=db(); alerts=screen_conflicts(c); menus=c.execute('SELECT * FROM menus ORDER BY day DESC,id DESC').fetchall(); c.close(); return render_template('screening.html',alerts=alerts,menus=menus)

@app.route('/menus/<int:mid>/approve',methods=['POST'])
@roles('admin','coordinator')
def approve_menu(mid):
    c=db(); c.execute('UPDATE menus SET approved=1 WHERE id=?',(mid,)); c.commit(); c.close(); log('approve menu',str(mid)); flash('Menu marked approved by the coordinator. This does not replace ingredient and allergy verification.','success'); return redirect(url_for('screening'))

@app.route('/recommendations',methods=['GET','POST'])
@roles('admin','coordinator','nurse')
def recommendations():
    c=db(); pupils=c.execute('SELECT * FROM pupils WHERE active=1 ORDER BY last_name').fetchall(); selected=request.values.get('pupil_id',type=int); recs=[]; pupil=None
    if selected:
        pupil=c.execute('SELECT * FROM pupils WHERE id=?',(selected,)).fetchone(); allergies=[r['allergen'] for r in c.execute('SELECT allergen FROM allergies WHERE pupil_id=?',(selected,)).fetchall()]
        options=[('Rice with steamed vegetables','rice, vegetables'),('Chicken with rice','chicken, rice'),('Egg sandwich','egg, wheat'),('Milk and fruit','milk, fruit')]
        for dish,ingredients in options:
            conflict=[a for a in allergies if a in [x.strip() for x in ingredients.split(',')]]
            recs.append({'dish':dish,'ingredients':ingredients,'conflict':conflict,'status':'FLAG — do not serve until reviewed' if conflict else 'Candidate for coordinator review'})
    c.close(); return render_template('recommendations.html',pupils=pupils,selected=selected,pupil=pupil,recs=recs)

@app.route('/forecast',methods=['GET','POST'])
@roles('admin','coordinator')
def forecast():
    c=db(); result=None
    if request.method=='POST':
        target=request.form.get('day',(date.today()+timedelta(days=1)).isoformat()); portion=float(request.form.get('rice_per_pupil','0.10')); days=int(request.form.get('history_days','7'))
        history=c.execute('SELECT day, SUM(meal_count) n FROM attendance WHERE day<? GROUP BY day ORDER BY day DESC LIMIT ?',(target,days)).fetchall(); avg=sum(r['n'] for r in history)/len(history) if history else c.execute('SELECT COUNT(*) n FROM pupils WHERE active=1').fetchone()['n']
        rice=max(0,round(avg*portion,2)); viand=max(0,round(avg)); method=f'Historical attendance average ({len(history)} days)'; notes='Estimate only. Coordinator must review against attendance, menu, inventory, and serving size.'
        c.execute('INSERT INTO forecasts(day,rice_kg,viand_portions,method,notes) VALUES(?,?,?,?,?)',(target,rice,viand,method,notes)); c.commit(); result={'day':target,'rice':rice,'viand':viand,'avg':round(avg,1),'method':method,'notes':notes}; log('create forecast',target)
    history=c.execute('SELECT * FROM forecasts ORDER BY day DESC,id DESC LIMIT 20').fetchall(); c.close(); return render_template('forecast.html',result=result,history=history)

@app.route('/inventory',methods=['GET','POST'])
@roles('admin','coordinator')
def inventory():
    c=db()
    if request.method=='POST':
        item=request.form.get('item','').strip(); unit=request.form.get('unit','kg'); qty=request.form.get('quantity',type=float)
        if not item or qty is None or qty<0: flash('Enter a valid item and non-negative quantity.','error')
        else:
            c.execute('INSERT INTO inventory(item,quantity,unit,updated_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(item) DO UPDATE SET quantity=excluded.quantity,unit=excluded.unit,updated_at=CURRENT_TIMESTAMP',(item,qty,unit)); c.commit(); log('update inventory',item); flash('Inventory updated.','success')
        c.close(); return redirect(url_for('inventory'))
    rows=c.execute('SELECT * FROM inventory ORDER BY item').fetchall(); c.close(); return render_template('inventory.html',items=rows)

@app.route('/reports')
@roles('admin','coordinator','nurse','teacher')
def reports():
    c=db(); counts={'pupils':c.execute('SELECT COUNT(*) n FROM pupils WHERE active=1').fetchone()['n'],'allergies':c.execute('SELECT COUNT(*) n FROM allergies').fetchone()['n'],'attendance':c.execute('SELECT COUNT(*) n FROM attendance WHERE day=? AND status="Present"',(date.today().isoformat(),)).fetchone()['n'],'menus':c.execute('SELECT COUNT(*) n FROM menus').fetchone()['n']}; c.close(); return render_template('reports.html',counts=counts)

@app.route('/reports/export/<kind>.csv')
@roles('admin','coordinator','nurse','teacher')
def export_report(kind):
    c=db(); queries={'pupils':('Pupil Records','SELECT student_id,first_name,last_name,grade,guardian,active FROM pupils ORDER BY last_name'), 'allergies':('Allergy Records','SELECT p.student_id,p.first_name,p.last_name,a.allergen,a.severity,a.verified,a.notes FROM allergies a JOIN pupils p ON p.id=a.pupil_id'), 'attendance':('Attendance Records','SELECT p.student_id,p.first_name,p.last_name,a.day,a.status,a.meal_count FROM attendance a JOIN pupils p ON p.id=a.pupil_id ORDER BY a.day DESC'), 'forecasts':('Meal Forecasts','SELECT day,rice_kg,viand_portions,method,notes FROM forecasts ORDER BY day DESC'), 'inventory':('Inventory','SELECT item,quantity,unit,updated_at FROM inventory')}
    if kind not in queries: c.close(); return 'Report type not found',404
    title,sql=queries[kind]; rows=c.execute(sql).fetchall(); c.close(); out=io.StringIO(); w=csv.writer(out); w.writerow([title]); w.writerow(rows[0].keys() if rows else ['No records']); [w.writerow(tuple(r)) for r in rows]
    resp=make_response(out.getvalue()); resp.headers['Content-Type']='text/csv; charset=utf-8'; resp.headers['Content-Disposition']=f'attachment; filename=nutrisafe_{kind}_{date.today().isoformat()}.csv'; return resp

@app.route('/users',methods=['GET','POST'])
@roles('admin')
def users():
    c=db()
    if request.method=='POST':
        username=request.form.get('username','').strip(); password=request.form.get('password',''); name=request.form.get('display_name','').strip(); role=request.form.get('role','teacher')
        if not username or len(password)<10 or not name or role not in ('admin','coordinator','nurse','teacher'): flash('Provide all fields; password must be at least 10 characters.','error')
        else:
            try: c.execute('INSERT INTO users(username,password,role,display_name) VALUES(?,?,?,?)',(username,password,role,name)); c.commit(); log('create user',username); flash('User created. Change this starter app to hashed passwords before production.','success')
            except sqlite3.IntegrityError: flash('Username already exists.','error')
        c.close(); return redirect(url_for('users'))
    rows=c.execute('SELECT id,username,role,display_name FROM users ORDER BY username').fetchall(); c.close(); return render_template('users.html',users=rows)

@app.route('/settings')
@login_required
def settings(): return render_template('settings.html')

@app.errorhandler(500)
def server_error(e): return render_template('error.html',message='An unexpected error occurred. Please return to the dashboard and try again.'),500

if __name__=='__main__':
    app.run(debug=True, host='127.0.0.1', port=5000)
