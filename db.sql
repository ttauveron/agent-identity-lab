CREATE TABLE employees (username text PRIMARY KEY, subject text NOT NULL, department text NOT NULL);
CREATE TABLE user_permissions (username text NOT NULL, capability text NOT NULL, PRIMARY KEY(username, capability));
CREATE TABLE agent_permissions (agent text NOT NULL, capability text NOT NULL, PRIMARY KEY(agent, capability));
CREATE TABLE invoices (id integer PRIMARY KEY, department text NOT NULL, description text NOT NULL, amount numeric NOT NULL, bank_account text NOT NULL);
CREATE TABLE payments (id integer PRIMARY KEY, invoice_id integer REFERENCES invoices(id), department text NOT NULL, amount numeric NOT NULL, status text NOT NULL);

INSERT INTO employees VALUES ('alice', 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'finance'), ('bob', 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', 'hr');
INSERT INTO user_permissions VALUES ('alice','invoices.read'),('alice','invoices.write'),('alice','payments.read'),('alice','payments.approve'),('bob','payments.read');
INSERT INTO agent_permissions VALUES ('finance-agent','invoices.read'),('finance-agent','invoices.write'),('finance-agent','payments.read');
INSERT INTO invoices VALUES (1,'finance','Laptop fleet',1200,'CH11-SECRET'),(2,'hr','Recruiting',500,'CH22-SECRET');
INSERT INTO payments VALUES (1,1,'finance',1200,'pending'),(2,2,'hr',500,'pending');
