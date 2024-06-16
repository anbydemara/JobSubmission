import sqlite3
import time

from flask import Flask, render_template, request, redirect, session, g, jsonify
import pandas as pd
import os
import shutil
import hashlib
import zipfile
import threading
from flask_paginate import Pagination

DATABASE = './submission.db'


app = Flask(__name__)
app.config['SECRET_KEY'] = 'xai-submission'


def make_dicts(cursor, row):
    return dict((cursor.description[idx][0], value)
                for idx, value in enumerate(row))


def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
    db.row_factory = make_dicts
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()


# 查询方法
def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    return (rv[0] if rv else None) if one else rv


# 更新
def update_db(sql, args=()):
    db = get_db()
    cur = get_db().cursor()
    cur.execute(sql, args)
    db.commit()
    cur.close()


# 插入
def insertMany(sql, data):
    db = get_db()
    cur = get_db().cursor()
    try:
        cur.executemany(sql, data)
        db.commit()
    except Exception as e:
        db.rollback()
    finally:
        cur.close()


# 密码进行md5加密
def encrypt(password):
    return hashlib.md5(password.encode('utf-8')).hexdigest()


# 全部已提交课程（id+name）
def getAllSubCourses():
    results = query_db('select distinct courseId from submit')
    for res in results:
        res['courseName'] = getCourseNameById(res['courseId'])
    return results


# 截至日期判断
def isLate(courseId):
    res = query_db('select deadline from course where courseId=?', [int(courseId)], True)
    if res is None:
        return True
    elif res['deadline'] <= int(round(time.time())):
        return True
    else:
        return False


# 生成目录
def creat_folder(folder_path):
    if not os.path.exists(folder_path):
        os.makedirs(folder_path)
        return False
    else:
        return True


# 根据课程id获取课程名称
def getCourseNameById(courseId):
    course = query_db("select courseName from course where courseId=?", [courseId], True)
    if course is None:
        return ''
    return course['courseName']


# 根据小组id获取课程id
def getCidByGid(groupId):
    res = query_db('select courseId from student where groupId=?', [groupId], True)
    return res['courseId']


# 管理员登录判断
def admin_is_login():
    if session.get('admin_id'):
        return True
    else:
        return False


# 获取课程菜单
def getMenu(allMenu=True):
    menu = {}
    if allMenu:
        courses = query_db("select * from course where list='已导入'")
    else:
        courses = query_db("select * from course where courseId in (select distinct courseId from student where submit='已提交')")
    for course in courses:
        schoolYear = course['schoolYear']
        term = course['term']
        key = ''.join([str(schoolYear), '年第', str(term), '学期'])
        elem = (course['courseId'], course['courseName'])
        if menu.get(key):
            temp = menu.get(key)
            temp.append(elem)
            menu[key] = temp
        else:
            menu[key] = [elem]
    for key, item in menu.items():
        menu[key] = item[::-1]
    return menu


@app.route('/')
def index():  # 程序入口
    lst = getAllSubCourses()
    for c in lst:
        c['subList'] = query_db('''select sub.groupId, stu.member, stu.project
                                   from student as stu, submit as sub 
                                   where stu.groupId=sub.groupId and sub.courseId=? 
                                   order by sub.subDate desc limit 3''', [c['courseId']])
    menu = getMenu(False)
    return render_template('temp.html', length=len(lst), lst=lst[::-1], menu=menu)


@app.route('/toLogin')
def toLogin():
    return render_template('login.html')


@app.route('/login', methods=['get', 'post'])
def login():  # 登录请求
    if request.method == 'GET':
        return redirect('/toLogin')
    else:
        groupId = request.form.get('account')
        password = request.form.get('password')
        password = encrypt(password)  # 加密

        user = query_db('select * from student where groupId=? and password=?',
                        [groupId, password], one=True)
        if user is None:
            return render_template('login.html', not_login="账号或密码错误")
        else:   # 用户名及密码正确
            courseId = getCidByGid(int(groupId))
            if not isLate(courseId):    # 未截至
                session['group_id'] = groupId
                return render_template('change.html', member=user['member'].split('_'), project=user['project'], groupId=groupId)
            else:
                return render_template('login.html', not_login="已到截至日期")


# 学生退出登录
@app.route('/logout', methods=['GET', 'POST'])
def log_out():
    if session.get('group_id'):
        session.pop('group_id')
    return redirect('/toLogin')


# 重置截至日期
@app.route('/set_deadline', methods=['post'])
def set_deadline():
    courseId = request.form.get('courseId')
    deadline = f"{request.form.get('year')}/{request.form.get('month')}/{request.form.get('day')} " \
               f"{request.form.get('hour')}:{request.form.get('minute')}:00"
    t = int(round(time.mktime(time.strptime(deadline, '%Y/%m/%d %X'))))
    update_db('update course set deadline=? where courseId=?', [t, int(courseId)])
    return redirect('/cmanage')


@app.route('/subStatus', methods=['GET', 'POST'])
def subStatus():
    groupId = request.args.get("groupId")
    if groupId is None:
        return jsonify(False)
    sub = query_db("select groupId from submit where groupId=?", [int(groupId)], True)
    if sub is None:
        return jsonify(False)
    return jsonify(True)


@app.route("/importStatus", methods=['get', 'post'])
def importStatus():
    courseId = request.args.get("courseId")
    if not courseId:
        return jsonify(False)
    res = query_db("select list from course where courseId=?", [int(courseId)], True)
    if res is None or res['list'] == '未导入':
        return jsonify(False)
    return jsonify(True)


# 导入学生名单
@app.route('/import', methods=['post'])
def import_stuList():  # 导入课程学生名单
    courseId = request.form.get('courseId')
    data = request.files
    df_list = pd.read_csv(data['stuListFile'], index_col=0)
    if [df_list.index.name] + [column for column in df_list] != ['group id', 'group member'] or df_list.empty:
        courses = query_db("select courseId,courseName from course")
        return render_template('importStuList.html', courses=courses[::-1], info="文件内容不合格")
    db = get_db()
    cur = get_db().cursor()
    try:
        # 删除该课程原来学生提交的作业（如果有）
        if os.path.exists(f'./static/data/{courseId}'):
            shutil.rmtree(f'./static/data/{courseId}')

        # 删除该课程原来学生的提交记录（如果有）
        cur.execute('delete from submit where courseId=?', [int(courseId)])

        # 删除该课程原来的学生（如果有）
        cur.execute('delete from student where courseId=?', [int(courseId)])

        # 保存名单到数据库
        df_list.index = df_list.index.astype('str')
        students = []
        for groupId in df_list.index:
            res = query_db('select groupId from student where groupId=?', [groupId], True)
            new_id = groupId
            if res is not None:
                new_id = courseId + new_id
            student = (int(new_id), encrypt(new_id), df_list.loc[groupId, 'group member'], int(courseId))
            students.append(student)
        cur.executemany("insert into student values (?, ?, ?, '-', ?, '未提交')", students)

        # 设置截至时间和导入状态
        deadline = f"{request.form.get('year')}/{request.form.get('month')}/{request.form.get('day')} " \
                   f"{request.form.get('hour')}:{request.form.get('minute')}:00"
        t = int(round(time.mktime(time.strptime(deadline, '%Y/%m/%d %X'))))
        cur.execute('update course set deadline=?, list=? where courseId=?', [t, '已导入', int(courseId)])
        db.commit()
    except Exception as e:
        db.rollback()
    finally:
        return redirect('/cmanage')


@app.route('/addOne', methods=['get'])
def addStudent():
    db = get_db()
    cur = get_db().cursor()
    try:
        cur.execute("insert into student values (?, ?, ?, '-', ?, '未提交')", (int('2107040107'), encrypt('2107040107'), "叶文萱", int('1003')))
        db.commit()
    except Exception as e:
        db.rollback()
    return redirect('/')


@app.route('/toUpload', methods=['post', 'get'])
def to_upload():
    if session.get('group_id'):
        group_id = session.get('group_id')
        courseId = getCidByGid(int(group_id))
        res = query_db('select deadline from course where courseId=?', [int(courseId)], True)
        my_time = time.localtime(res['deadline'])
        res_time = [my_time.tm_year, my_time.tm_mon, my_time.tm_mday, my_time.tm_hour, my_time.tm_min]
        return render_template('upload.html', group_id=group_id, time=res_time)
    else:
        return redirect('/toLogin')


@app.route('/upload', methods=['post', 'get'])
def file_save():  # 上传作业
    if request.method == 'POST':
        group = request.form.get('group_id')
        courseId = getCidByGid(int(group))  # 课程编号

        file_dir = f'static/data/{courseId}/{group}'
        creat_folder(file_dir)

        data = request.files
        video = data['video']
        if video:
            video.save(f'{file_dir}/main.mp4')
        ppt = data['ppt']
        if ppt:
            ppt.save(f'{file_dir}/report.pptx')
        report = data['report']
        if report:
            report.save(f'{file_dir}/report.pdf')
        code = data['code']
        if code:
            code.save(f'{file_dir}/code.zip')
        picture = data['picture']
        if picture:
            picture.save(f'{file_dir}//main.png')

        res = query_db("select groupId from submit where groupId=?", [int(group)], True)
        db = get_db()
        cur = get_db().cursor()
        try:
            if res is None:
                # 增加提交记录
                cur.execute('insert into submit values (NULL, ?, ?, ?)', [int(group), int(courseId), int(round(time.time()))])
            else:
                cur.execute("update submit set subDate=? where groupId=?", [int(round(time.time())), int(group)])
            # 修改提交状态
            cur.execute("update student set submit='已提交' where groupId=?", [int(group)])
            db.commit()
        except Exception as e:
            db.rollback()
        finally:
            cur.close()
            return redirect('/home?course=' + str(courseId))  # 重定向到展示界面（直接展示提交的课程所有提交记录）
    else:
        return redirect('/toLogin')


@app.route('/home')
def home():  # 去作业展示页
    if request.args.get('course'):
        course = request.args.get('course')
        courseName = getCourseNameById(int(course))
        menu = getMenu(False)

        res = query_db('''select sub.groupId, stu.member, stu.project
                                           from student as stu, submit as sub 
                                           where stu.groupId=sub.groupId and sub.courseId=? 
                                           order by sub.subDate desc''', [int(course)])
        return render_template('home.html', data=res, data_length=len(res), course=course, courseName=courseName, menu=menu)
    else:
        return redirect('/')


@app.route('/admin', methods=['post', 'get'])
def admin_login():  # 管理员登录
    if request.method == 'POST':
        username = request.form.get('admin')
        password = request.form.get('password')
        admin = query_db("select * from admin where username=? and password=?", [username, encrypt(password)], True)
        if admin is None:
            not_login = "账号或密码错误"
            return render_template('login.html', not_login=not_login)
        session['admin_id'] = username
        return redirect('/management')
    else:
        if session.get('admin_id'):
            return redirect('/management')
        else:
            return redirect('/toLogin')


@app.route('/management', methods=['post', 'get'])
def management(limit=8):
    if session.get('admin_id'):
        username = session.get('admin_id')
        data = query_db("select groupId,member,project,c.courseId,c.courseName,submit from student as stu inner join course c on stu.courseId=c.courseId ")
        menu = getMenu()
        data = data[::-1]
        page = int(request.args.get("page", 1))
        start = (page - 1) * limit
        end = page * limit if len(data) > page * limit else len(data)
        paginate = Pagination(page=page, per_page=limit, total=len(data), css_framework='bootstrap5')
        return render_template('management.html', data=data[start:end], admin_id=username, menu=menu, paginate=paginate)
    else:
        return redirect('/toLogin')


# 管理员退出登录
@app.route('/admin_logout', methods=['get', 'post'])
def logout():
    if session.get('admin_id'):
        session.pop('admin_id')
    return redirect('/toLogin')


@app.route('/remove', methods=['post', 'get'])
def remove():  # 管理员删除小组上传资料
    if request.method == 'GET':
        return redirect('/toLogin')
    if not admin_is_login():     # 管理员未登录
        return redirect('/toLogin')
    group_id = request.form.get('group_id')
    courseId = getCidByGid(int(group_id))
    if os.path.exists(f'./static/data/{courseId}/{group_id}'):
        # 删除文件
        shutil.rmtree(f'./static/data/{courseId}/{group_id}')
        db = get_db()
        cur = db.cursor()
        try:
            # 修改提交状态
            cur.execute("update student set submit='未提交' where groupId=?", [int(group_id)])
            # 删除提交记录
            cur.execute("delete from submit where groupId=?", [int(group_id)])
            db.commit()
        except Exception as e:
            db.rollback()
        finally:
            cur.close()
    return redirect('/show_course?course=' + str(courseId))


@app.route('/manage/upload', methods=['post', 'get'])
def upload_pro():  # 管理员处上传小组资料（仅作重定向）
    if request.method == 'GET':
        return redirect('/toLogin')
    else:
        groupId = request.form.get('group_id')
        session['group_id'] = groupId
        courseId = getCidByGid(int(groupId))
        res = query_db('select deadline from course where courseId=?', [int(courseId)], True)
        my_time = time.localtime(res['deadline'])
        res_time = [my_time.tm_year, my_time.tm_mon, my_time.tm_mday, my_time.tm_hour, my_time.tm_min]
        return render_template('upload.html', group_id=groupId, time=res_time)


@app.route('/reset', methods=['post'])
def reset():  # 修改密码
    groupId = request.form.get('groupId')
    oldPassword = request.form.get('oldPassword')
    newPassword = request.form.get('newPassword')
    res = query_db("select password from student where groupId=?", [int(groupId)], True)
    if res is None:
        not_login = '该用户不存在'
        return render_template('login.html', not_login=not_login)
    if res['password'] != encrypt(oldPassword):
        not_login = '原密码错误'
        return render_template('login.html', not_login=not_login)
    update_db("update student set password=? where groupId=?", [encrypt(newPassword), int(groupId)])
    not_login = '密码修改成功'
    return render_template('login.html', not_login=not_login)


@app.route('/infoReset', methods=['get', 'post'])
def InfoReset():  # 重定向到修改小组信息页面（附带小组信息）
    if session.get("group_id"):
        groupId = session.get("group_id")
        student = query_db("select member,project from student where groupId=?", [int(groupId)], True)
        if student is None:
            return redirect('/toLogin')
        return render_template('change.html', member=student['member'].split('_'), project=student['project'], groupId=groupId)
    else:
        return redirect("/toLogin")


@app.route('/resetInfo', methods=['post'])
def Info_Reset():  # 修改小组信息
    if not session.get("group_id"):
        return redirect("/toLogin")
    project = request.form.get('project')
    groupId = request.form.get('groupId')

    member_lst = [request.form.get('headMan'), request.form.get('member1'), request.form.get('member2'), request.form.get('member3')]
    while '' in member_lst:
        member_lst.remove('')
    member = '_'.join(member_lst)
    update_db("update student set project=?,member=? where groupId=?", [project, member, int(groupId)])

    return redirect('/toUpload')


@app.route('/toAddStu')
def toAddStudent():  # 去导入学生名单页面
    if session.get('admin_id'):
        courses = query_db("select courseId,courseName from course")
        return render_template('importStuList.html', courses=courses[::-1], info="")
    else:
        return redirect('/toLogin')


@app.route('/show_course')
def show_course(limit=7):  # 显示单个课程的学生名单
    if (session.get('admin_id')):
        if request.args.get('course'):
            course = request.args.get('course')
            data = query_db('select groupId,member,submit from student where courseId=?', [int(course)])

            # 分页
            data = data[::-1]
            page = int(request.args.get("page", 1))
            start = (page - 1) * limit
            end = page * limit if len(data) > page * limit else len(data)
            paginate = Pagination(page=page, per_page=limit, total=len(data), css_framework='bootstrap5')

            for stu in data:
                sub = query_db("select subDate from submit where groupId=?", [stu['groupId']], True)
                stu['subDate'] = '--'
                if sub is not None:
                    stu['subDate'] = time.strftime("%Y/%m/%d %X", time.localtime(sub['subDate']))
            # 新增信息汇总
            res = query_db("select count(*) as total from student where courseId=?", [int(course)], True)
            total_count = res['total']
            res = query_db("select count(*) as total from submit where courseId=?", [int(course)], True)
            sub_count = res['total']
            noSub_count = total_count - sub_count
            info = [total_count, sub_count, noSub_count, course, getCourseNameById(int(course))]
            return render_template('courseOne.html', data=data[start:end], info=info, menu=getMenu(), paginate=paginate)
        else:
            return redirect('/management')
    else:
        return redirect('/toLogin')


def package(dirPath, outFullPath, courseId):
    zip = zipfile.ZipFile(outFullPath, "w", zipfile.ZIP_DEFLATED)
    for path, dirnames, filenames in os.walk(dirPath):
        fpath = path.replace(dirPath, '')
        for filename in filenames:
            zip.write(os.path.join(path, filename), os.path.join(fpath, filename))
    zip.close()
    db = sqlite3.connect(DATABASE)
    cur = db.execute("select packaged from package where courseId=? limit 1", [int(courseId)])
    res = cur.fetchall()
    if not len(res):
        # packaged : 0:未打包，1:已打包
        cur.execute("insert into package values (NULL, ?, 1)", [int(courseId)])
    else:
        cur.execute("update package set packaged=1 where courseId=?", [int(courseId)])
    db.commit()
    cur.close()
    db.close()
    return


@app.route('/package', methods=['GET', 'POST'])
def packageData():
    courseId = request.args.get('courseId')
    if not courseId:
        return jsonify(False)
    dirPath = f'./static/data/{courseId}'
    outPath = './static/package/'
    if not os.path.exists(outPath):
        creat_folder(outPath)
    outFullPath = f'./static/package/{courseId}.zip'
    t = threading.Thread(target=package, args=(dirPath, outFullPath, courseId))
    t.start()
    return jsonify(True)


@app.route('/packStatus', methods=['GET', 'POST'])
def packStatus():
    courseId = request.args.get("courseId")
    if not courseId:
        return jsonify(False)
    res = query_db("select packaged from package where courseId=?", [int(courseId)], True)
    if res is None:
        return jsonify(False)
    if res['packaged'] == 1 and os.path.exists(f'./static/package/{courseId}.zip'):
        return jsonify(True)
    return jsonify(False)


@app.route('/delPackage', methods=['GET', 'POST'])
def delPackage():
    courseId = request.form.get('courseId')
    if not courseId:
        return redirect(request.referrer)
    dirPath = f'./static/package/{courseId}.zip'
    if os.path.exists(dirPath):
        os.remove(dirPath)
    update_db("update package set packaged=0 where courseId=?", [int(courseId)])
    return redirect(request.referrer)


# 课程管理部分
@app.route('/cmanage')  # 转到课程管理界面
def to_course_manage(limit=5):
    if session.get('admin_id'):
        courses = query_db('select * from course')
        # 分页
        courses = courses[::-1]
        page = int(request.args.get("page", 1))
        start = (page - 1) * limit
        end = page * limit if len(courses) > page * limit else len(courses)
        paginate = Pagination(page=page, per_page=limit, total=len(courses), css_framework='bootstrap5')
        courses = courses[start:end]
        for course in courses:
            if course['list'] == '未导入':
                course['ratio'] = '0/0'
            else:
                res1 = query_db('select count(*) as total from student where courseId=?', [course['courseId']], True)
                res2 = query_db('select count(*) as total from student where courseId=? and submit=?', [course['courseId'], '已提交'], True)
                course['ratio'] = f"{res2['total']}/{res1['total']}"
            if course['deadline']:
                course['deadline'] = time.strftime("%Y/%m/%d %H:%M", time.localtime(course['deadline']))
            else:
                course['deadline'] = '--'
        return render_template('coursemanage.html', courses=courses, paginate=paginate)
    else:
        return redirect('/toLogin')


@app.route('/toaddcourse')  # 转到添加课程界面
def to_add_course():
    if session.get('admin_id'):
        return render_template('addCourse.html', info="")
    else:
        return redirect('/toLogin')


@app.route('/insert_course', methods=['GET', 'POST'])
def insert_course():  # 单个课程新增
    if request.method == "GET":
        return redirect('/toLogin')
    if not admin_is_login():     # 管理员未登录
        return redirect('/toLogin')
    courseName = request.form.get('courseName')
    schoolYear = request.form.get('schoolYear')
    term = request.form.get('term')
    grade = request.form.get('grade')
    update_db("insert into course values(NULL, ?, ?, ?, ?, '未导入', NULL)", [courseName, schoolYear, term, grade])
    return redirect('/cmanage')


@app.route('/listin_course', methods=['GET', 'POST'])
def insert_course_list():  # 多个课程新增（文件）
    if request.method == "GET":
        return redirect('/toLogin')
    if not admin_is_login():     # 管理员未登录
        return redirect('/toLogin')
    data = request.files
    df = pd.read_csv(data['courseList'])
    if [column for column in df] != ['courseName', 'schoolYear', 'term', 'grade']:
        return render_template('addCourse.html', info="文件内容不正确，添加失败")
    courses = []
    for i, row in df.iterrows():
        course = (row['courseName'], row['schoolYear'], row['term'], row['grade'])
        courses.append(course)
    if len(courses):
        insertMany("insert into course values (NULL, ?, ?, ?, ?, '未导入', NULL)", courses)
    return redirect('/cmanage')


@app.route('/removeCourse', methods=['GET', 'POST'])
def removeCourse():  # 删除整个课程
    if request.method == "GET":
        return redirect('/toLogin')
    if not admin_is_login():     # 管理员未登录
        return redirect('/toLogin')
    courseId = request.form.get('courseId')
    db = get_db()
    cur = get_db().cursor()
    try:
        # 删除course中的记录
        cur.execute("delete from course where courseId=?", [int(courseId)])

        # 删除提交记录submit
        cur.execute("delete from submit where courseId=?", [int(courseId)])

        # 删除课程为courseId的学生账号
        cur.execute("delete from student where courseId=?", [int(courseId)])

        # 删除打包
        cur.execute("delete from package where courseId=?", [int(courseId)])
        db.commit()
        # 删除提交资料data
        dataPath = f'./static/data/{courseId}'
        if os.path.exists(dataPath):
            shutil.rmtree(dataPath)
        packPath = f'./static/package/{courseId}.zip'
        if os.path.exists(packPath):
            os.remove(packPath)
    except Exception as e:
        db.rollback()
    finally:
        cur.close()
        return redirect('/cmanage')


@app.route('/toChangeCourse')
def toChangeCourse():
    if request.args.get('course'):
        courseId = request.args.get('course')
        course = query_db("select courseName,schoolYear,term,grade from course where courseId=?", [int(courseId)], True)
        if course is None:
            return redirect('/cmanage')
        str_list = course['schoolYear'].split('-')
        courseInfo = [courseId, course['courseName'], str_list[0], str_list[1],
                     course['term'], course['grade']]
        return render_template('changeCourse.html', courseInfo=courseInfo)
    else:
        return redirect('/cmanage')


@app.route('/changeCourse', methods=['GET', 'POST'])
def changeCourse():
    if session.get("admin_id"):
        courseId = request.form.get('courseId')
        courseName = request.form.get('courseName')
        schoolYear = request.form.get('schoolYear')
        term = request.form.get('term')
        grade = request.form.get('grade')

        # 修改course
        update_db("update course set courseName=?,schoolYear=?,term=?,grade=? where courseId=?", [courseName, schoolYear, term, grade, int(courseId)])
        return redirect('/cmanage')
    else:
        return redirect("/toLogin")


@app.route("/toReset", methods=['GET', 'POST'])
def toReset():
    if session.get("admin_id"):
        return render_template("reset.html", adminId=session.get("admin_id"), status="")
    else:
        return redirect("/toLogin")


@app.route("/resetAdmin", methods=['POST'])
def resetAdmin():
    if session.get("admin_id"):
        username = request.form.get("username")
        password = request.form.get("password")
        origin = session.get("admin_id")
        admin = query_db("select username from admin where username=?", [origin], True)
        if admin is None:
            return redirect("/toLogin")
        else:
            update_db("update admin set username=?,password=? where username=?", [username, encrypt(password), origin])
            session['admin_id'] = username
            return render_template("reset.html", adminId=username, status="重置成功")
    else:
        return redirect("/toLogin")

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)
    # app.run(debug=True)
