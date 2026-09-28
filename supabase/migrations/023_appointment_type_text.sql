-- The appointment_type enum is too narrow — services are defined dynamically per org
-- in the JSONB services column. Convert to text so any service type can be stored.
ALTER TABLE public.appointments
  ALTER COLUMN type TYPE text USING type::text;

DROP TYPE IF EXISTS public.appointment_type;
