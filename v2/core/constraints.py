"""A composite foreign key, declared in `Meta.constraints` (design 4.0).

Django's ForeignKey can only reference one column. Tenant rows must reference
rows of the same clinic, so the real constraint is

    FOREIGN KEY (clinic_id, patient_id) REFERENCES patients_patient (clinic_id, id)

The ForeignKey field stays (with db_constraint=False) for the ORM; this class
makes the database enforce that both rows belong to the same clinic. The
migration autodetector adds and removes it like any other constraint.
"""

from django.db.models.constraints import BaseConstraint


class CompositeForeignKey(BaseConstraint):
    def __init__(self, *, fields, to, to_fields, name, on_delete="RESTRICT", violation_error_message=None):
        if len(fields) != len(to_fields) or len(fields) < 2:
            raise ValueError("CompositeForeignKey needs two or more fields, matching to_fields.")
        if on_delete not in ("RESTRICT", "NO ACTION", "CASCADE"):
            raise ValueError("on_delete must be RESTRICT, NO ACTION or CASCADE.")
        super().__init__(name=name, violation_error_message=violation_error_message)
        self.fields = tuple(fields)
        self.to = to
        self.to_fields = tuple(to_fields)
        self.on_delete = on_delete

    def _target(self, model):
        # `model._meta.apps` is the migration state's registry inside migrations.
        return model._meta.apps.get_model(self.to)

    def _columns(self, model, names):
        return [model._meta.get_field(name).column for name in names]

    def create_sql(self, model, schema_editor):
        quote = schema_editor.quote_name
        target = self._target(model)
        local = ", ".join(quote(c) for c in self._columns(model, self.fields))
        remote = ", ".join(quote(c) for c in self._columns(target, self.to_fields))
        return (
            f"ALTER TABLE {quote(model._meta.db_table)} ADD CONSTRAINT {quote(self.name)} "
            f"FOREIGN KEY ({local}) REFERENCES {quote(target._meta.db_table)} ({remote}) "
            f"ON DELETE {self.on_delete}"
        )

    def constraint_sql(self, model, schema_editor):
        # Inside CREATE TABLE the referenced table may not exist yet, so the
        # constraint is added once every table of the migration is created.
        schema_editor.deferred_sql.append(self.create_sql(model, schema_editor))
        return None

    def remove_sql(self, model, schema_editor):
        quote = schema_editor.quote_name
        return f"ALTER TABLE {quote(model._meta.db_table)} DROP CONSTRAINT {quote(self.name)}"

    def validate(self, model, instance, exclude=None, using=None):
        # The database checks it; services never rely on model validation.
        return None

    def deconstruct(self):
        path, args, kwargs = super().deconstruct()
        kwargs.update(fields=self.fields, to=self.to, to_fields=self.to_fields)
        if self.on_delete != "RESTRICT":
            kwargs["on_delete"] = self.on_delete
        return path, args, kwargs

    def __eq__(self, other):
        if not isinstance(other, CompositeForeignKey):
            return NotImplemented
        return (self.name, self.fields, self.to.lower(), self.to_fields, self.on_delete) == (
            other.name, other.fields, other.to.lower(), other.to_fields, other.on_delete)

    def __repr__(self):
        return f"<CompositeForeignKey {self.name}: {self.fields} -> {self.to}{self.to_fields}>"
