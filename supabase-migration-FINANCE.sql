-- Add Finance permission to profiles table
ALTER TABLE profiles ADD COLUMN can_finance BOOLEAN DEFAULT false;
