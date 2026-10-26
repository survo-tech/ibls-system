-- Fields the phone team needs next to each firm (from business_sample.csv, joined on CIN).
ALTER TABLE contacts ADD COLUMN city TEXT;
ALTER TABLE contacts ADD COLUMN company_phone TEXT;
ALTER TABLE contacts ADD COLUMN director_names TEXT;
ALTER TABLE contacts ADD COLUMN director_mobiles TEXT;
CREATE INDEX IF NOT EXISTS idx_contacts_cin ON contacts(cin);
