from collections import defaultdict

from django.apps import apps
from django.db import connections, router
from django.db.utils import DatabaseError

from .fields import _TRIGGERS  # noqa


def get_trigger_name(field):
    """

    :param field: Field instance
    :return: unicode
    """
    if field._trigger_name:
        name = field._trigger_name
    else:
        name = '{1.db_table}_{0.name}'.format(field, field.model._meta)
    return 'concurrency_{}'.format(name)


def get_triggers(databases=None):
    if databases is None:
        databases = [alias for alias in connections]

    ret = {}
    for alias in databases:
        connection = connections[alias]
        f = factory(connection)
        r = f.get_list()
        ret[alias] = r
    return ret


def drop_triggers(*databases):
    global _TRIGGERS
    ret = defaultdict(lambda: [])
    for app_label, model_name in _TRIGGERS:
        model = apps.get_model(app_label, model_name)
        field = model._concurrencymeta.field
        alias = router.db_for_write(model)
        if alias in databases:
            connection = connections[alias]
            f = factory(connection)
            f.drop(field)
            field._trigger_exists = False
            ret[alias].append([model, field, field.trigger_name])
        else:  # pragma: no cover
            pass
    return ret


def create_triggers(databases):
    global _TRIGGERS
    ret = defaultdict(lambda: [])

    for app_label, model_name in _TRIGGERS:
        model = apps.get_model(app_label, model_name)
        field = model._concurrencymeta.field
        storage = model._concurrencymeta.triggers
        alias = router.db_for_write(model)
        if (alias in databases) and field not in storage:
            storage.append(field)
            connection = connections[alias]
            f = factory(connection)
            f.create(field)
            ret[alias].append([model, field, field.trigger_name])
        else:  # pragma: no cover
            pass

    return ret


class TriggerFactory(object):
    drop_clause = ""
    list_clause = ""

    def __init__(self, connection):
        self.connection = connection

    def get_update_clause(self, trigger_name, opts, field):
        raise NotImplementedError()

    def get_trigger(self, field):
        if field.trigger_name in self.get_list():
            return field.trigger_name
        return None

    def create(self, field):
        if field.trigger_name not in self.get_list():
            stm = self.get_update_clause(
                trigger_name=field.trigger_name,
                opts=field.model._meta,
                field=field,
            )
            try:
                self.connection.cursor().execute(stm)
            except BaseException as exc:  # pragma: no cover
                raise DatabaseError("""Error executing:
{1}
{0}""".format(exc, stm))
        else:  # pragma: no cover
            pass
        field._trigger_exists = True

    def drop(self, field):
        opts = field.model._meta
        ret = []
        stm = self.drop_clause.format(trigger_name=field.trigger_name,
                                      opts=opts,
                                      field=field)
        self.connection.cursor().execute(stm)
        ret.append(field.trigger_name)
        return ret

    def _list(self):
        cursor = self.connection.cursor()
        cursor.execute(self.list_clause)
        return cursor.fetchall()

    def get_list(self):
        return sorted([m[0] for m in self._list()])


class Sqlite3(TriggerFactory):
    drop_clause = """DROP TRIGGER IF EXISTS {trigger_name};"""

    list_clause = "select name from sqlite_master where type='trigger';"

    def get_update_clause(self, trigger_name, opts, field):
        q = """CREATE TRIGGER {trigger_name}
AFTER UPDATE ON {opts.db_table}
BEGIN UPDATE {opts.db_table} SET {field.column} = {field.column}+1 WHERE {opts.pk.column} = NEW.{opts.pk.column};
END;""".format(trigger_name=trigger_name, opts=opts, field=field)
        return q



class PostgreSQL(TriggerFactory):
    drop_clause = r"""DROP TRIGGER IF EXISTS {trigger_name} ON {opts.db_table};"""

    list_clause = "select * from pg_trigger where tgname LIKE 'concurrency_%%'; "

    def get_update_clause(self, trigger_name, opts, field):
        q = r"""CREATE OR REPLACE FUNCTION func_{trigger_name}()
    RETURNS TRIGGER as
    '
    BEGIN
       NEW.{field.column} = OLD.{field.column} +1;
        RETURN NEW;
    END;
    ' language 'plpgsql';

CREATE TRIGGER {trigger_name} BEFORE UPDATE
    ON {opts.db_table} FOR EACH ROW
    EXECUTE PROCEDURE func_{trigger_name}();
    """.format(trigger_name=trigger_name, opts=opts, field=field)
        return q

    def get_list(self):
        return sorted([m[1] for m in self._list()])


class MySQL(TriggerFactory):
    drop_clause = """DROP TRIGGER IF EXISTS {trigger_name};"""

    list_clause = "SHOW TRIGGERS"

    def get_update_clause(self, trigger_name, opts, field):
        clause = """
        CREATE TRIGGER {trigger_name} BEFORE UPDATE ON {opts.db_table}
        FOR EACH ROW BEGIN {set_clause} END;
        """

        set_clause = "SET NEW.{field.column} = OLD.{field.column}+1;".format(field=field)
        if field._check_fields:
            cond_clause = " OR ".join(
                ["NEW.{0} <> OLD.{0}".format(db_field) for db_field in field._check_fields])

            set_clause = "IF {cond_clause} THEN {set_clause} END IF;".format(
                cond_clause=cond_clause,
                set_clause=set_clause
            )

        return clause.format(
            trigger_name=trigger_name,
            opts=opts,
            set_clause=set_clause
        )

def factory(conn):
    try:
        return {
            'postgresql': PostgreSQL,
            'mysql': MySQL,
            'sqlite3': Sqlite3,
            'sqlite': Sqlite3,
        }[conn.vendor](conn)
    except KeyError:  # pragma: no cover
        raise ValueError('{} is not supported by TriggerVersionField'.format(conn))
