import {PrismaClient,AdminRole} from '@prisma/client';
const db=new PrismaClient();
async function main(){const id=BigInt(process.env.OWNER_TELEGRAM_ID||'0');if(!id)throw new Error('OWNER_TELEGRAM_ID required');const u=await db.user.upsert({where:{telegramId:id},update:{status:'ACTIVE'},create:{telegramId:id,username:'owner'}});await db.admin.upsert({where:{userId:u.id},update:{role:AdminRole.OWNER,active:true},create:{userId:u.id,role:AdminRole.OWNER,permissions:['*']}});console.log('Owner seeded:',u.id.toString())}main().finally(()=>db.$disconnect());
