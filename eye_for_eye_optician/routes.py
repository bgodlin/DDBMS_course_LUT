import os
import secrets
from PIL import Image
from eye_for_eye_optician import app, bcrypt, mail
from flask import render_template, flash, redirect, url_for, session, request, jsonify
from eye_for_eye_optician.forms import *
from eye_for_eye_optician.models import *
from flask_login import login_user, current_user, logout_user, login_required
from flask_mail import Message
import requests
import jwt

class Token:
    token = jwt.encode({'hardware_id': app.config["ID"]}, app.config['SECRET_KEY'])


@app.route("/")
@app.route("/home")
def home():
    if current_user.is_authenticated:
        page = request.args.get('page', 1, type=int)
        created_cases = Case.query.filter_by(optician=current_user.id) \
            .order_by(Case.date_posted.desc()).paginate(page=page, per_page=5)
        return render_template('home.html', created_cases=created_cases)

    return render_template('home.html')


@app.route("/about")
def about():
    return render_template('about.html', title='About')


# --------------------------------------- Registration, Login, Logout ---------------------------------------

@app.route("/register_optician", methods=['GET', 'POST'])
def register_optician():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    form = RegistrationForm()
    if form.validate_on_submit():
        hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')

        try:
            opt_id = requests.post(
                app.config["QP"] + '/register_optician',
                headers={"x-access-token": Token.token},
                json={"name": form.name.data,
                      "surname": form.surname.data,
                      "email": form.email.data,
                      "password": hashed_password}).json()

            optician = Optician(id=opt_id["created_id"], name=form.name.data, surname=form.surname.data,
                                email=form.email.data,
                                password=hashed_password)

            db.session.add(optician)
            db.session.commit()
            flash(f'Account created for {form.email.data}!', 'success')
            return redirect(url_for('login_optician'))

        except KeyError:
            return opt_id

    return render_template('register_optician.html', title='Register', form=form)


@app.route("/login_optician", methods=['GET', 'POST'])
def login_optician():
    form = LoginForm()
    if form.validate_on_submit():
        optician = Optician.query.filter_by(email=form.email.data).first()
        if optician:
            if optician.active == True:
                if bcrypt.check_password_hash(optician.password, form.password.data):
                    login_user(optician, remember=form.remember.data)
                    return redirect(url_for('home'))
                else:
                    flash('Login Unsuccessful. Please check email and password', 'danger')
            else:
                flash('User has not been activated yet', 'danger')
        else:
            flash('No user with the submitted email was found. Please, check it again', 'danger')
    return render_template('login.html', title='Login', form=form)


@app.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('home'))


# Case creation

@app.route('/create_case', methods=['GET', 'POST'])
@login_required
def step1():
    form = CitizenSearchForm()
    if form.validate_on_submit():
        citizen = Citizen.query.filter_by(email=form.email.data).first()

        # If notfound in the local check the main

        if not citizen:
            try:
                citizen = requests.get(
                    app.config["QP"] + '/find_citizen',
                    headers={"x-access-token": Token.token},
                    json={"email": form.email.data}).json()

                found_citizen = Citizen(id=citizen["id"], name=citizen["name"], surname=citizen["surname"],
                                        email=citizen["email"],
                                        date_of_birth=citizen["date_of_birth"],
                                        phone_number=citizen["phone_number"],
                                        # TODO image transfer
                                        image_file=citizen["image_file"],
                                        country=citizen["country"])

                db.session.add(found_citizen)
                db.session.commit()

                citizen = found_citizen

            # Error if not found anywhere

            except KeyError:
                error_message = citizen['message']
                flash(f'{error_message}', 'danger')
                return redirect(url_for('step1'))

        session['citizen_id'] = citizen.id
        return redirect(url_for('step2'))

    return render_template('step1.html', form=form)


def find_free_ophtalmologist():
    # TODO find ophto which hasn't got any assigned cases
    free_ophtalmologist_query = """
    select free_ophta.id, count(*) as "Cases"
    from "case" as "cases"
         inner join
         (select * from "ophtalmologist" where active = True and available = True) "free_ophta"
         on cases.ophtalmologist = free_ophta.id
    group by free_ophta.id
    order by 2
    limit 1
    """
    free_ophtalmologist = db.engine.execute(free_ophtalmologist_query).first().id
    return free_ophtalmologist


def generate_case_code(citizen, current_time):
    # citizen_region_key = Country.query.filter_by(id=citizen.country).first().key
    current_date, time = current_time.strftime("%Y%m%d"), current_time.strftime("%H%M%S")
    return f"FI-{citizen.name[0]}{citizen.surname[0]}-{current_date}-{time}"


def save_case_files(form_picture):
    random_hex = secrets.token_hex(8)
    _, f_ext = os.path.splitext(form_picture.filename)
    picture_fn = random_hex + f_ext
    picture_path = str(os.path.join('eye_for_eye_optician/static/cases', picture_fn))

    # output_size = (125, 125)
    i = Image.open(form_picture)
    # i.thumbnail(output_size)
    i.save(picture_path)
    return picture_path


@app.route('/step2', methods=['GET', 'POST'])
@login_required
def step2():
    form = OpticianUploadForm()
    found_citizen = session.get('citizen_id', None)
    citizen = Citizen.query.filter_by(id=found_citizen).first()

    image_file = url_for('static', filename='citizens/' + citizen.image_file)

    if form.validate_on_submit():
        current_time = datetime.utcnow()
        case_code = generate_case_code(citizen, current_time)
        filenames = []
        for file in form.files.data:
            picture_file = save_case_files(file)
            filenames.append(picture_file)
        files = {}
        for file in range(len(filenames)):
            files['file{}'.format(file)] = open(filenames[file], 'rb')

        try:
            request = requests.post(
                app.config["QP"] + '/register_case',
                headers={"x-access-token": Token.token,
                         "optician_comment": form.optician_comment.data,
                         "citizen": str(citizen.id),
                         "code": case_code,
                         "optician": str(current_user.id)
                         },
                files=files
            ).json()

            # TODO currently whole image path is saved to the db

            case = Case(id=request['case_id'], code=case_code, citizen=citizen.id, optician=current_user.id,
                        status=1, optician_comment=form.optician_comment.data,
                        images=filenames)

            db.session.add(case)
            db.session.commit()

            flash('New case have been created', 'success')
            return redirect(url_for('home'))

        except Exception:
            flash(f'Failed to save a case', 'danger')
            return redirect(url_for('step2'))

    return render_template('step2.html', image_file=image_file, citizen=citizen, form=form)


@app.route("/register_new_citizen", methods=['GET', 'POST'])
@login_required
def register_new_citizen():
    form = CitizenRegistrationForm()
    if form.validate_on_submit():
        picture_file = save_citizen_picture(form.picture.data)
        file = {'file': open('eye_for_eye_optician/static/citizens/' + picture_file, 'rb')}

        try:
            request = requests.post(
                app.config["QP"] + '/register_citizen',
                headers={"x-access-token": Token.token,
                         "name": form.name.data,
                         "surname": form.surname.data,
                         "email": form.email.data,
                         "date_of_birth": str(form.date_of_birth.data),
                         "phone_number": form.phone_number.data
                         },
                files=file
            ).json()

            new_citizen = Citizen(id=request["created_id"], name=form.name.data, surname=form.surname.data,
                                  email=form.email.data, date_of_birth=form.date_of_birth.data,
                                  phone_number=form.phone_number.data,
                                  image_file=picture_file)

            db.session.add(new_citizen)
            db.session.commit()
            flash(f'Citizen with email {form.email.data} was registered!', 'success')
            return redirect(url_for('step1'))

        except KeyError:
            error_message = request['message']
            flash(f'{error_message}', 'danger')

        new_citizen = Citizen(name=form.name.data, surname=form.surname.data, date_of_birth=form.date_of_birth.data,
                              email=form.email.data, phone_number=form.phone_number.data, image_file=picture_file)

        db.session.add(new_citizen)
        db.session.commit()
        flash('New citizen has been created!', 'success')
        return redirect(url_for('step1'))

    return render_template('citizen_registration.html', title='Citizen', form=form)


# Optician's account

def save_profile_picture(form_picture):
    random_hex = secrets.token_hex(8)
    _, f_ext = os.path.splitext(form_picture.filename)
    picture_fn = random_hex + f_ext
    picture_path = os.path.join(app.root_path, 'static/profile_pics', picture_fn)

    # output_size = (125, 125)
    i = Image.open(form_picture)
    # i.thumbnail(output_size)
    i.save(picture_path)
    return picture_fn


def save_citizen_picture(form_picture):
    random_hex = secrets.token_hex(8)
    _, f_ext = os.path.splitext(form_picture.filename)
    picture_fn = random_hex + f_ext
    picture_path = os.path.join(app.root_path, 'static/citizens', picture_fn)

    # output_size = (125, 125)
    i = Image.open(form_picture)
    # i.thumbnail(output_size)
    i.save(picture_path)
    return picture_fn


@app.route("/account", methods=['GET', 'POST'])
@login_required
def account():
    form = UpdateAccountForm()
    if form.validate_on_submit():
        if form.picture.data:
            picture_file = save_profile_picture(form.picture.data)
            current_user.image_file = picture_file
        db.session.commit()
        flash('Your account has been updated!', 'success')
        return redirect(url_for('account'))
    # elif request.method == 'GET':
    #     form.username.data = current_user.username
    #     form.email.data = current_user.email
    image_file = url_for('static', filename='profile_pics/' + current_user.image_file)
    return render_template('account.html', title='Account',
                           image_file=image_file, form=form)


# Reset password

def send_reset_email(user):
    token = user.get_reset_token()
    msg = Message('Password Reset Request',
                  sender=app.config['MAIL_USERNAME'],
                  recipients=[user.email])
    msg.body = f'''Optician intranet.
To reset your password, visit the following link:
{url_for('reset_token', token=token, _external=True)}
If you did not make this request then simply ignore this email and no changes will be made.
'''
    mail.send(msg)


@app.route("/reset_password", methods=['GET', 'POST'])
def reset_request():
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    form = RequestResetForm()
    if form.validate_on_submit():
        user = Optician.query.filter_by(email=form.email.data).first()
        send_reset_email(user)
        flash('An email has been sent with instructions to reset your password.', 'info')
        return redirect(url_for('login_optician'))
    return render_template('reset_request.html', title='Reset Password', form=form)


@app.route("/reset_password/<token>", methods=['GET', 'POST'])
def reset_token(token):
    if current_user.is_authenticated:
        return redirect(url_for('home'))
    user = Optician.verify_reset_token(token)
    if user is None:
        flash('That is an invalid or expired token', 'warning')
        return redirect(url_for('reset_request'))
    form = ResetPasswordForm()
    if form.validate_on_submit():
        hashed_password = bcrypt.generate_password_hash(form.password.data).decode('utf-8')
        user.password = hashed_password
        db.session.commit()
        flash('Your password has been updated! You are now able to log in', 'success')
        return redirect(url_for('login_optician'))
    return render_template('reset_token.html', title='Reset Password', form=form)
