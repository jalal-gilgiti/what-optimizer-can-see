create function q6conditions(shipdate date,
                             discount decimal(12,2),
                             qty int)
returns int as $$
declare stdateChar varchar = '1994-01-01';
declare stdate date = cast(stdateChar as date);
declare newdate date = stdate + (INTERVAL '1 YEAR');
declare val decimal(12,2) = 0.06;
declare epsilon decimal(12,2) = 0.01;
declare lowerbound decimal(12,2);
declare upperbound decimal(12,2);
begin
    if(shipdate < stdateChar) then
        return 0;
    end if;
    if(shipdate >= newdate) then
        return 0;
    end if;
    if(qty >= 24) then
        return 0;
    end if;
    lowerbound = val - epsilon;
    upperbound = val + epsilon;
    if(discount >= lowerbound AND discount <= upperbound) then
        return 1;
    end if;
    return 0;
end $$
LANGUAGE PLPGSQL;
