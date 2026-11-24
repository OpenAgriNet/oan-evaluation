-- Synthetic bulletin data for the existing IMD DB query; never production data.
CREATE FUNCTION imd_lookup(keys text[]) RETURNS jsonb LANGUAGE sql AS $$
  SELECT jsonb_build_object(
    'districts', jsonb_build_array(jsonb_build_object('name', 'Patna', 'lgd_district_code', 230)),
    'date', current_date::text,
    'valid_until', (current_date + 2)::text,
    'advisories', jsonb_build_array(jsonb_build_object(
      'crop', 'Rice',
      'advisory_eng', 'Synthetic bulletin: inspect drainage before rainfall and avoid spraying during rain.',
      'advisory_reg', ''
    ))
  );
$$;
