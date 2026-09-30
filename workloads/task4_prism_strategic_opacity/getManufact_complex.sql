CREATE FUNCTION getManufact_complex(itm INT)
RETURNS VARCHAR(50)
AS
$$
DECLARE
    man VARCHAR(50); t VARCHAR(50);
    cnt1 int; cnt2 int;
BEGIN
    man := '';
    IF itm > 500 THEN
        cnt1 = 100;
    ELSE
        cnt1 = 101;
    END IF;
    cnt1 := cnt1 + (SELECT COUNT(*) FROM generate_series(1,100))::INTEGER;
    cnt2 := cnt2 + (SELECT COUNT(*) FROM generate_series(1,100))::INTEGER;
    IF (cnt1 > 0 AND cnt2 > 0) THEN
        man := (SELECT (SELECT 'abc' FROM generate_series(1,100)));
    ELSE
        man := 'outdated item';
    END IF;
    RETURN man;
END
 $$ LANGUAGE PLPGSQL;
